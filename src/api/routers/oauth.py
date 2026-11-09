"""OAuth 2.1 authorization server for MCP clients: browser sign-in, PKCE, refresh.

A client such as Claude Code or a claude.ai custom connector connects with the
URL alone. It discovers this server from the 401 challenge of the MCP endpoint
(``.well-known/oauth-protected-resource``, ``oauth_metadata.py``), registers
itself, and sends the person to the web app to sign in and approve:

    POST /oauth/register              anonymous dynamic client registration
                                      (RFC 7591), PUBLIC clients only
    GET  /oauth/authorize             validates the request, then redirects the
                                      browser to the web app's consent page
    GET  /oauth/consent-info          what the consent page shows (bearer)
    POST /oauth/authorize/consent     Allow / Deny, answered as JSON
                                      ``{redirect_to}`` (bearer)
    POST /oauth/token                 authorization_code + PKCE, refresh_token
                                      (rotating, reuse revokes the family) and
                                      client_credentials (confidential only)
    GET  /oauth/connections           "Connected apps": a person's own, or a
                                      workspace administrator's workspace
    DELETE /oauth/connections/{id}    revoke one connection
    GET|POST|DELETE /oauth/clients    administrator-registered clients (the
                                      only way to get a confidential client)

Design:
  * The consent page lives in the web app, behind its normal sign-in guard, and
    calls ``/oauth/authorize/consent`` with the person's own API token. A form
    post from the browser carries no such token, which is why consent is a
    bearer-authenticated JSON call and not a form.
  * The redirect is built here, from the redirect URI the client REGISTERED and
    that the request matched; the page only navigates to what the server returns.
  * Auth code TTL = 60 s, single use (claimed with one conditional UPDATE).
  * Access token = the MCP JWT (``src.mcp_server.auth.issue_token``, about an
    hour), so the verifier that guards ``/mcp`` accepts it. It names the
    ``mcp_tokens`` row it runs under (``grant`` claim): revoking that row, or the
    person losing the workspace, ends it at once.
  * Refresh token: opaque, stored hashed, rotated on every use. A token used
    twice revokes its whole family (RFC 9700 section 4.14).
  * Scopes are read-only unless a write scope is asked for AND the person's role
    in the chosen workspace may use it (``oauth_rules.scope_plan``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import oauth_rules as rules
from src.api.deps import current_workspace_id, get_current_user, require_workspace_admin
from src.db.models import (
    OAuthAuthCode,
    OAuthClient,
    OAuthRefreshToken,
    Workspace,
    WorkspaceMember,
)
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])

_hasher = PasswordHasher()
_CODE_TTL_SECONDS = 60
_TOKEN_TTL_SECONDS = 3600
_REFRESH_TTL_SECONDS = 30 * 24 * 3600   # 30 days, renewed by every rotation

#: A client registered anonymously that never completed a grant is removed after this.
_UNUSED_CLIENT_TTL = timedelta(hours=24)
#: A ceiling on anonymous registrations alive at once, so the table cannot be filled.
_ANONYMOUS_CLIENT_CAP = 2000
# One address may register this many clients per `_UNUSED_CLIENT_TTL` (counted
# per process; no address is stored): a single noisy caller cannot fill the
# shared cap of waiting registrations and lock everyone else out.
_PER_IP_UNUSED_CAP = 100
_RECENT_BY_IP: dict[str, list[float]] = {}
_RECENT_IPS_MAX = 5000


def _caller_address(request: Request) -> str:
    """The peer address, as the rate limiter reads it: behind the one trusted proxy
    the LAST X-Forwarded-For entry (the leftmost is the caller's to forge)."""
    ip = request.client.host if request.client else "unknown"
    if os.environ.get("CELMIS_TRUST_PROXY", "").strip() == "1":
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if parts:
            ip = parts[-1]
    return ip


def _address_key(ip: str | None) -> str:
    """The unit a registration is counted against: an IPv4 address, or the /64 of an
    IPv6 one (a single subscriber holds a whole /64, so per-address would be no cap)."""
    import ipaddress

    try:
        addr = ipaddress.ip_address((ip or "").strip().strip("[]"))
    except ValueError:
        return ip or "unknown"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _registration_allowed(ip: str | None) -> bool:
    """Record one registration from ``ip``; False when it already has too many."""
    now = time.monotonic()
    horizon = now - _UNUSED_CLIENT_TTL.total_seconds()
    if len(_RECENT_BY_IP) >= _RECENT_IPS_MAX:
        for key in [k for k, v in _RECENT_BY_IP.items() if not v or v[-1] < horizon]:
            del _RECENT_BY_IP[key]
        if len(_RECENT_BY_IP) >= _RECENT_IPS_MAX:
            # Still full of live entries: drop the quietest tenth, not everyone.
            oldest = sorted(_RECENT_BY_IP, key=lambda k: _RECENT_BY_IP[k][-1])
            for k in oldest[: max(1, _RECENT_IPS_MAX // 10)]:
                del _RECENT_BY_IP[k]
    key = _address_key(ip)
    stamps = [t for t in _RECENT_BY_IP.get(key, ()) if t >= horizon]
    if len(stamps) >= _PER_IP_UNUSED_CAP:
        _RECENT_BY_IP[key] = stamps
        return False
    stamps.append(now)
    _RECENT_BY_IP[key] = stamps
    return True


# ─── Which workspace a grant was given in ───────────────────────────
#
# An access token answers for the workspace the person consented in (the MCP
# server reads its `workspace_id` claim — see src/mcp_server/identity.py), so
# that workspace has to survive two hops: consent → code, and code → refresh
# family → every rotated access token.
#
# Neither table has a column for it, and a migration for one string is not
# worth a schema change on the auth path. Both hops already carry a value
# the SERVER generates and looks up by exact match — the auth code (primary
# key) and the refresh family id (never shown to the client) — so the
# workspace is appended to that value. A client cannot alter it: a changed
# code matches no row, and the family id never leaves the database. Values
# minted before this existed carry no suffix and yield None, which the MCP
# server treats as a legacy token.

_WS_SEP = "."   # not in the token_urlsafe / token_hex alphabets


def _bind_workspace(opaque: str, workspace_id: str | None) -> str:
    if not workspace_id:
        return opaque
    tag = base64.urlsafe_b64encode(workspace_id.encode()).rstrip(b"=").decode()
    return f"{opaque}{_WS_SEP}{tag}"


def _bound_workspace(opaque: str | None) -> str | None:
    if not opaque or _WS_SEP not in opaque:
        return None
    tag = opaque.rsplit(_WS_SEP, 1)[1]
    try:
        value = base64.urlsafe_b64decode(tag + "=" * (-len(tag) % 4)).decode()
    except Exception:  # noqa: BLE001 — a malformed tag is a legacy value
        return None
    return value or None


def _workspace_claims(workspace_id: str | None, grant_id: str | None = None,
                      family_id: str | None = None) -> dict | None:
    claims: dict = {}
    if family_id:
        # The session this token belongs to. The MCP server asks whether it was
        # ended (refresh family revoked) on every call, so disconnecting an app
        # stops its access token even when it runs under an administrator's
        # shared grant, which revoking a connection deliberately leaves alone.
        claims["family"] = family_id
    if workspace_id:
        claims["workspace_id"] = workspace_id
    if grant_id:
        # The row that says which repos this token may reach. The MCP server
        # looks it up on every call, so revoking or narrowing it applies at once.
        claims["grant"] = grant_id
    return claims or None


async def _grant_id(session: AsyncSession, user_id: str, workspace_id: str | None) -> str | None:
    """Id of the person's active ``oauth_grant`` row in the workspace, or None.

    OAuth is not a way around the per-person token list: without a grant a
    superadmin created, there is nothing to consent to."""
    if not workspace_id:
        return None
    from src.mcp_server import token_store

    def _find(sync_session):  # noqa: ANN001, ANN202
        row = token_store.active_grant(
            sync_session, user_id=user_id, workspace_id=workspace_id)
        return row.id if row is not None else None

    return await session.run_sync(_find)


_NO_GRANT = ("No MCP access has been granted to you for this workspace. "
             "Ask your superadmin to grant it.")


_NO_GRANT = ("MCP access is not open to you on this workspace. An administrator "
             "issues it per person (or turns on CELMIS_MCP_SELF_SERVICE so members "
             "can connect themselves).")


# ─── Refusals ───────────────────────────────────────────────────────


class OAuthRefusal(Exception):
    """A refusal on an OAuth endpoint, answered in the RFC 6749 / 7591 error shape.

    FastAPI's own errors are `{"detail": ...}`. An OAuth client does not read
    that: the official MCP SDK looks for `error` and `error_description`, and
    when they are missing it reports a schema-validation dump instead of the
    sentence. The handler below (`oauth_refusal_handler`, installed by
    `build_app`) writes both, plus `detail` for anything that reads the usual
    shape (the web app's consent page does).
    """

    def __init__(self, status_code: int, error: str, description: str) -> None:
        super().__init__(description)
        self.status_code = status_code
        self.error = error
        self.description = description


async def oauth_refusal_handler(request: Request, exc: OAuthRefusal) -> JSONResponse:  # noqa: ARG001
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "error_description": exc.description,
                 "detail": exc.description},
        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
    )


def _bad_request(error: str, description: str) -> OAuthRefusal:
    return OAuthRefusal(400, error, description)


def _no_store(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    """A token response is never cached (RFC 6749 section 5.1)."""
    return JSONResponse(payload, status_code=status_code,
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _issuer(request: Request) -> str:
    from src.mcp_server.public_url import base_from_request

    return base_from_request(request)


def _web_origin(request: Request) -> str:
    """The public origin of the web app, where the consent page lives.

    The API's public base is the web origin plus the proxy prefix
    (`https://host/backend`), so the origin is that base without its path. With
    no prefix (an API on its own domain) the web app is `PUBLIC_BASE_URL`.
    """
    base = urlsplit(_issuer(request))
    if not base.path.strip("/"):
        from src.config import get_settings

        configured = str(getattr(get_settings(), "public_base_url", "") or "").strip()
        parts = urlsplit(configured)
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return f"{base.scheme}://{base.netloc}"


# ─── Anonymous dynamic client registration (RFC 7591) ───────────────

_REGISTER_GRANTS = {"authorization_code", "refresh_token"}


async def _collect_unused_clients(session: AsyncSession) -> int:
    """Delete clients that registered themselves and never completed a grant.

    Anonymous registration is open, so an abandoned attempt (a client that
    registered and whose user never finished signing in) must not stay forever.
    A client is kept once any code or refresh token names it, and a client an
    administrator registered (`created_by` set) is never touched.
    """
    cutoff = datetime.now(UTC) - _UNUSED_CLIENT_TTL
    used_codes = select(OAuthAuthCode.client_id)
    used_refresh = select(OAuthRefreshToken.client_id)
    result = await session.execute(
        delete(OAuthClient).where(
            OAuthClient.created_by.is_(None),
            OAuthClient.created_at < cutoff,
            OAuthClient.client_id.not_in(used_codes),
            OAuthClient.client_id.not_in(used_refresh),
        )
    )
    await session.commit()
    count = int(result.rowcount or 0)
    if count:
        logger.info("oauth_unused_clients_removed count=%d", count)
    return count


@router.post("/register", status_code=201)
async def register_client(
    request: Request,
    payload: Any = Body(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    """Dynamic client registration for MCP clients (RFC 7591), anonymous.

    Open to anyone, which is why it can only create the least powerful thing: a
    PUBLIC client. No secret exists, `token_endpoint_auth_method` has to be
    `none`, and every redirect URI is loopback (any port) or https on an
    operator-allowed host (`OAUTH_ALLOWED_REDIRECT_HOSTS`). A registration gives
    nobody access: a person still has to sign in and approve it, and sees the
    client's name and redirect host when they do. A client that never gets
    approved is deleted after a day. The route is rate-limited with the token
    endpoint (`CELMIS_RL_AUTH`).

    A confidential client (a secret, `client_credentials`) is registered by a
    workspace administrator at `POST /oauth/clients`.
    """
    if not isinstance(payload, dict):
        raise _bad_request("invalid_client_metadata", "send a JSON object")

    method = payload.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise _bad_request(
            "invalid_client_metadata",
            "only public clients can register themselves: token_endpoint_auth_method "
            "must be 'none'. A workspace administrator registers a confidential client "
            "at POST /oauth/clients.")

    uris = payload.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 5:
        raise _bad_request("invalid_redirect_uri", "redirect_uris must list one to five URIs")
    for uri in uris:
        problem = rules.redirect_problem(uri)
        if problem:
            raise _bad_request("invalid_redirect_uri", problem)

    grants = payload.get("grant_types") or ["authorization_code"]
    if (not isinstance(grants, list) or not set(grants) <= _REGISTER_GRANTS
            or "authorization_code" not in grants):
        raise _bad_request(
            "invalid_client_metadata",
            "grant_types may be authorization_code and refresh_token")
    responses = payload.get("response_types") or ["code"]
    if responses != ["code"]:
        raise _bad_request("invalid_client_metadata", "response_types must be ['code']")

    await _collect_unused_clients(session)
    if not _registration_allowed(_caller_address(request)):
        raise OAuthRefusal(429, "temporarily_unavailable",
                           "too many registrations from this address; try again later")
    # Only clients nobody has approved count: one that completed a grant is in
    # use and must not push newcomers out, however many there are.
    alive = await session.scalar(
        select(func.count()).select_from(OAuthClient).where(
            OAuthClient.created_by.is_(None),
            OAuthClient.client_id.not_in(select(OAuthAuthCode.client_id)),
            OAuthClient.client_id.not_in(select(OAuthRefreshToken.client_id))))
    if (alive or 0) >= _ANONYMOUS_CLIENT_CAP:
        raise OAuthRefusal(503, "temporarily_unavailable",
                           "too many unapproved clients are waiting; try again later")

    name = rules.clean_client_name(payload.get("client_name"))
    client_id = f"ec_{uuid.uuid4().hex[:16]}"
    row = OAuthClient(
        client_id=client_id, client_secret_hash=None, name=name,
        redirect_uris=list(dict.fromkeys(uris)),
        # The ceiling a person can approve from. Which of them a token gets is
        # decided at consent by the person's role (`oauth_rules.scope_plan`).
        allowed_scopes=rules.known_scopes(),
        created_by=None,
    )
    session.add(row)
    await session.commit()
    logger.info("oauth_client_registered id=%s name=%s public=True anonymous=True", client_id, name)
    return _no_store({
        "client_id": client_id,
        "client_id_issued_at": int(datetime.now(UTC).timestamp()),
        "client_name": name,
        "redirect_uris": row.redirect_uris,
        "grant_types": sorted(set(grants)),
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": " ".join(row.allowed_scopes),
    }, 201)


# ─── Administrator-registered clients (confidential allowed) ────────


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
    # Every session the client holds ends with it, and so do the per-connection
    # grants those sessions ran under.
    tokens = (await session.scalars(
        select(OAuthRefreshToken).where(OAuthRefreshToken.client_id == client_id)
    )).all()
    await _revoke_grants(session, tokens, by=user.id)
    for t in tokens:
        t.revoked = True
    await session.delete(row)
    await session.commit()
    logger.info("oauth_client_deleted id=%s tokens_revoked=%d by=%s",
                client_id, len(tokens), user.email)


@router.post("/clients", response_model=RegisterClientOut, status_code=201)
async def register_admin_client(
    payload: RegisterClientIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
) -> RegisterClientOut:
    """Register a client, public or confidential, as a workspace administrator.

    Workspace admin rather than platform admin. A client is bound to whoever
    registered it — the MCP identity resolver reads `created_by` and gives the
    token that person's workspace — so registering one grants no authority the
    registrant does not already have by clicking in the UI.

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
        name=rules.clean_client_name(payload.name),
        redirect_uris=list(payload.redirect_uris),
        allowed_scopes=list(payload.allowed_scopes),
        created_by=user.email,
    )
    session.add(row)
    await session.commit()
    logger.info("oauth_client_registered id=%s name=%s public=%s by=%s",
                client_id, row.name, payload.public, user.email)
    return RegisterClientOut(
        client_id=client_id, client_secret=secret_plain,
        name=row.name, redirect_uris=row.redirect_uris,
        allowed_scopes=row.allowed_scopes,
    )


# ─── /authorize: hand the browser to the web app ────────────────────


async def _validated(
    session: AsyncSession, *, client_id: str, redirect_uri: str, code_challenge: str,
    method: str, scope: str, resource: str | None, base: str,
) -> tuple[OAuthClient, list[str], str | None]:
    """Everything about an authorization request that does not depend on WHO is
    asking. Runs at /authorize, at the consent page's lookup and again at the
    consent POST, which is directly callable: no step is authoritative by having
    been seen earlier. Returns the client, the requested scopes and the resource."""
    if method != "S256":
        # RFC 7636 section 4.3: an unsupported method is invalid_request. `plain`
        # is refused on purpose: its challenge IS the verifier, so anything that
        # can read the authorize URL (history, a proxy log, a referrer) could
        # redeem the code. The S256 hash cannot be reversed.
        raise _bad_request("invalid_request", "code_challenge_method must be S256"
                           + (f" ({method!r} is not supported)" if method else ""))
    client = await session.get(OAuthClient, client_id)
    if client is None:
        raise _bad_request("invalid_request", "unknown client_id")
    if not rules.redirect_matches(list(client.redirect_uris or []), redirect_uri):
        raise _bad_request("invalid_request", "redirect_uri is not registered for this client")
    if not rules.valid_challenge(code_challenge):
        raise _bad_request("invalid_request", "code_challenge is not a valid S256 challenge")
    requested = rules.split_scope(scope)
    # Fail-CLOSED: every requested scope must be one this server knows AND one
    # the client may ask for. A client with an empty allowlist may request none.
    unknown = [s for s in requested if s not in rules.known_scopes()]
    if unknown:
        raise _bad_request("invalid_scope", f"unknown scope: {', '.join(unknown)}")
    allowed = set(client.allowed_scopes or [])
    denied = [s for s in requested if s not in allowed]
    if denied:
        raise _bad_request("invalid_scope", f"scopes not allowed for client: {denied}")
    problem = rules.resource_problem(resource, base)
    if problem:
        raise _bad_request("invalid_target", problem)
    return client, requested, rules.normalise_resource(resource)


@router.get("/authorize")
async def authorize(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
    scope: str = Query(default=""),
    state: str = Query(default="", max_length=2048),
    resource: str | None = Query(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> Response:
    """Validate the request, then send the browser to the web app's consent page.

    The API does not render a page and does not sign anybody in: the person's
    browser holds the web app's session, so the consent page is there, behind
    its normal sign-in guard (an anonymous visitor goes through /login, SSO
    included, and comes back with this query intact). The page asks
    `/oauth/consent-info` what to show and posts the decision to
    `/oauth/authorize/consent`.
    """
    if response_type != "code":
        raise _bad_request("unsupported_response_type", "only response_type=code is supported")
    await _validated(
        session, client_id=client_id, redirect_uri=redirect_uri,
        code_challenge=code_challenge, method=code_challenge_method, scope=scope,
        resource=resource, base=_issuer(request))
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri,
        "code_challenge": code_challenge, "code_challenge_method": "S256",
        "scope": scope, "state": state,
    }
    if resource:
        params["resource"] = resource
    return RedirectResponse(
        f"{_web_origin(request)}/oauth/consent?{urlencode(params)}", status_code=302,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


# ─── The consent page's two calls ───────────────────────────────────


async def _workspace_option(session: AsyncSession, ws_id: str) -> dict[str, Any]:
    ws = await session.get(Workspace, ws_id)
    if ws is None:
        return {"id": ws_id, "slug": ws_id, "name": ws_id, "role": None}
    return {"id": ws.id, "slug": ws.slug, "name": ws.name, "role": None}


async def _choose_workspace(
    session: AsyncSession, request: Request, user: User, slug: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The workspace the grant is for, and every one the person could pick.

    A slug names one explicitly, and only a member (or a global admin) gets it.
    Without one the active workspace is used — the same resolution every other
    endpoint uses, so the cookie the web app's switcher sets counts."""
    rows = (await session.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(Workspace.name))).all()
    options = [{"id": w.id, "slug": w.slug, "name": w.name, "role": role} for w, role in rows]
    wanted = (slug or "").strip().lower()
    if wanted:
        pick = next((o for o in options if o["slug"] == wanted or o["id"].lower() == wanted), None)
        if pick is None and user.is_admin:
            ws = (await session.scalars(select(Workspace).where(
                (Workspace.slug == wanted) | (func.lower(Workspace.id) == wanted)))).first()
            if ws is not None:
                pick = {"id": ws.id, "slug": ws.slug, "name": ws.name, "role": None}
        if pick is None:
            # the active workspace the server itself resolved for this person (a platform
            # administrator who is not a member): the page echoes back what it was shown
            active = str(await current_workspace_id(request=request, user=user))
            if wanted == active.lower():
                pick = await _workspace_option(session, active)
        if pick is None:
            raise OAuthRefusal(403, "access_denied", "You are not a member of that workspace.")
        return pick, options
    ws_id = await current_workspace_id(request=request, user=user)
    pick = next((o for o in options if o["id"] == ws_id), None) or await _workspace_option(session, ws_id)
    return pick, options


async def _connect_block(session: AsyncSession, user: User, workspace_id: str) -> str | None:
    """Why this person may not connect an MCP client on this workspace, or None.

    Browser sign-in is not a way around the policy for MCP tokens: it opens
    when an administrator already granted the person MCP access here (an
    `oauth_grant`), when the person may issue tokens themselves (the
    superadmin), or when the install turned self-service on
    (`CELMIS_MCP_SELF_SERVICE`)."""
    if await _grant_id(session, user.id, workspace_id):
        return None
    from src.mcp_server import token_store

    if token_store.self_service_enabled() or token_store.may_issue(user):
        return None
    return _NO_GRANT


async def _plan(
    session: AsyncSession, request: Request, user: User, client: OAuthClient,
    requested: list[str], redirect_uri: str, slug: str | None,
) -> dict[str, Any]:
    pick, options = await _choose_workspace(session, request, user, slug)
    granted, withheld = rules.scope_plan(requested, pick["role"], is_admin=bool(user.is_admin))
    reasons = dict(withheld)
    shown = requested or granted
    return {
        "client": {"client_id": client.client_id, "name": client.name,
                   "redirect_host": rules.host_of(redirect_uri),
                   "registered_by_administrator": client.created_by is not None},
        "workspace": pick,
        "workspaces": options,
        "scopes": [{"scope": s, "granted": s in granted, "reason": reasons.get(s)} for s in shown],
        "granted": granted,
        "blocked": await _connect_block(session, user, pick["id"]),
    }


@router.get("/consent-info")
async def consent_info(
    request: Request,
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
    scope: str = Query(default=""),
    resource: str | None = Query(default=None),
    workspace: str | None = Query(default=None),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> JSONResponse:
    """What the consent page shows: who is asking, from which host, for what, and
    in which workspace. Same validation as /authorize; changes nothing."""
    client, requested, _res = await _validated(
        session, client_id=client_id, redirect_uri=redirect_uri,
        code_challenge=code_challenge, method=code_challenge_method, scope=scope,
        resource=resource, base=_issuer(request))
    plan = await _plan(session, request, user, client, requested, redirect_uri, workspace)
    return _no_store(plan)


class ConsentIn(BaseModel):
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str = "S256"
    scope: str = ""
    state: str = Field(default="", max_length=2048)
    resource: str | None = None
    workspace: str | None = None
    decision: Literal["allow", "deny"]
    model_config = ConfigDict(extra="forbid")


def _redirect_with(client: OAuthClient, given: str, params: dict[str, str], issuer: str) -> str:
    """``redirect_uri`` with the answer added to its query.

    Only ever called with a URI that matched the client's registration. `iss`
    (RFC 9207) tells the client which server answered, which is what stops a
    client that talks to several servers from redeeming this code at another."""
    redirect_uri = rules.canonical_redirect(list(client.redirect_uris or []), given)
    if redirect_uri is None:  # cannot happen after `_validated`; never fall back to the caller's text
        raise _bad_request("invalid_request", "redirect_uri is not registered for this client")
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, v) for k, v in params.items() if v != ""]
    query.append(("iss", issuer))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


@router.post("/authorize/consent")
async def authorize_consent(
    request: Request,
    payload: ConsentIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> JSONResponse:
    """The person pressed Allow or Deny on the web app's consent page.

    Authenticated with the person's own API bearer token, which a browser form
    post cannot carry, so this is called by the page with `fetch`, never by a
    cross-site form. Answers ``{"redirect_to": ...}`` and the page navigates
    there. The URL is built here from the registered redirect URI the request
    matched, never taken from the caller. Allow mints a 60 s single-use code
    bound to the workspace (`_bind_workspace`), the client, the PKCE challenge,
    the redirect URI and the resource."""
    issuer = _issuer(request)
    client, requested, resource = await _validated(
        session, client_id=payload.client_id, redirect_uri=payload.redirect_uri,
        code_challenge=payload.code_challenge, method=payload.code_challenge_method,
        scope=payload.scope, resource=payload.resource, base=issuer)

    if payload.decision == "deny":
        logger.info("oauth_consent_denied client=%s user=%s", client.client_id, user.email)
        return _no_store({"redirect_to": _redirect_with(
            client, payload.redirect_uri,
            {"error": "access_denied", "state": payload.state,
             "error_description": "The user denied the request"}, issuer)})

    plan = await _plan(session, request, user, client, requested, payload.redirect_uri,
                       payload.workspace)
    if plan["blocked"]:
        raise OAuthRefusal(403, "access_denied", plan["blocked"])
    workspace_id = plan["workspace"]["id"]
    code = _bind_workspace(secrets.token_urlsafe(32), workspace_id)
    session.add(OAuthAuthCode(
        code=code, client_id=client.client_id, user_id=user.id,
        redirect_uri=payload.redirect_uri, code_challenge=payload.code_challenge,
        code_challenge_method="S256", scope=" ".join(plan["granted"]), resource=resource,
        consumed=False,
        expires_at=datetime.now(UTC) + timedelta(seconds=_CODE_TTL_SECONDS),
    ))
    await session.commit()
    logger.info("oauth_code_issued client=%s user=%s ws=%s scopes=%s",
                client.client_id, user.email, workspace_id, plan["granted"])
    return _no_store({"redirect_to": _redirect_with(
        client, payload.redirect_uri, {"code": code, "state": payload.state}, issuer)})


# ─── /token ─────────────────────────────────────────────────────────


async def _authenticate_client(
    session: AsyncSession, client_id: str, client_secret: str | None,
) -> OAuthClient:
    client = await session.get(OAuthClient, client_id)
    if client is None:
        raise OAuthRefusal(401, "invalid_client", "unknown client")
    # Confidential-client secret check (a public client has no secret to check).
    if client.client_secret_hash:
        if not client_secret:
            raise OAuthRefusal(401, "invalid_client", "client_secret required")
        try:
            _hasher.verify(client.client_secret_hash, client_secret)
        except VerifyMismatchError:
            # A wrong secret is expected traffic, not an internal fault.
            raise OAuthRefusal(401, "invalid_client", "invalid client_secret") from None
    return client


def _jwt_config():  # noqa: ANN202
    try:
        from src.mcp_server.auth import JwtConfig
        return JwtConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        raise OAuthRefusal(500, "server_error",
                           f"OAuth server not fully configured: {exc}") from exc


@router.post("/token")
async def token_exchange(
    request: Request,
    grant_type: str = Form(...),
    code: str = Form(None),
    redirect_uri: str = Form(None),
    client_id: str = Form(...),
    client_secret: str = Form(None),
    code_verifier: str = Form(None),
    refresh_token: str = Form(None),
    resource: str = Form(None),
    session: AsyncSession = Depends(get_async_session),
) -> JSONResponse:
    client = await _authenticate_client(session, client_id, client_secret)
    if grant_type == "refresh_token":
        body = await _refresh_grant(
            request, client=client, refresh_token=refresh_token, resource=resource,
            session=session)
    elif grant_type == "client_credentials":
        body = await _client_credentials_grant(client=client)
    elif grant_type == "authorization_code":
        body = await _code_grant(
            request, client=client, code=code, redirect_uri=redirect_uri,
            code_verifier=code_verifier, resource=resource, session=session)
    else:
        raise _bad_request(
            "unsupported_grant_type",
            "grant_type must be authorization_code, refresh_token or client_credentials")
    return _no_store(body)


_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")   # RFC 7636 section 4.1


def _pkce_ok(verifier: str | None, challenge: str) -> bool:
    if not verifier or not _VERIFIER.fullmatch(verifier):
        return False
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed.encode(), challenge.encode())


async def _revoke_replayed_code(session: AsyncSession, code: str, client_id: str) -> None:
    """A code presented twice means somebody else holds it (RFC 6749 section 4.1.2):
    end the sessions it produced. A code carries no link to the family it created,
    but it is single use with a short life, so the family it made was created in the
    span from its issue to its expiry. The client's sessions for that person started
    inside that span (plus a little slack) are revoked; a later one, such as a second
    workspace connected afterwards, is not."""
    row = await session.get(OAuthAuthCode, code)
    if row is None or not row.consumed or row.client_id != client_id:
        return
    # A few seconds of slack: `issued_at` is stamped by the database, whose clock
    # and resolution are not the code row's.
    slack = timedelta(seconds=5)
    until = _aware(row.expires_at) + slack
    since = _aware(row.expires_at) - timedelta(seconds=_CODE_TTL_SECONDS) - slack
    rows = list((await session.scalars(
        select(OAuthRefreshToken).where(
            OAuthRefreshToken.client_id == client_id,
            OAuthRefreshToken.user_id == row.user_id,
            OAuthRefreshToken.revoked.is_(False)))).all())
    rows = [r for r in rows if since <= _aware(r.issued_at) <= until]
    for r in rows:
        r.revoked = True
    await session.commit()
    await _revoke_grants(session, rows, by="oauth")
    logger.warning("oauth_code_replayed client=%s user=%s revoked=%d",
                   client_id, row.user_id, len(rows))


async def _code_grant(
    request: Request, *, client: OAuthClient, code: str | None, redirect_uri: str | None,
    code_verifier: str | None, resource: str | None, session: AsyncSession,
) -> dict[str, Any]:
    if not code:
        raise _bad_request("invalid_request", "code required")
    if not code_verifier:
        raise _bad_request("invalid_request", "code_verifier required")
    # Claim the code with ONE conditional update: of two requests presenting it,
    # exactly one flips `consumed`, so a code is redeemed once even under a race.
    # It is claimed before it is judged, so a wrong verifier burns it too: a
    # thief holding the code gets one guess, not a series.
    claim = await session.execute(
        update(OAuthAuthCode)
        .where(OAuthAuthCode.code == code, OAuthAuthCode.consumed.is_(False))
        .values(consumed=True))
    await session.commit()
    if claim.rowcount != 1:
        await _revoke_replayed_code(session, code, client.client_id)
        raise _bad_request("invalid_grant", "invalid or consumed code")
    row = await session.get(OAuthAuthCode, code)
    if row is None:
        raise _bad_request("invalid_grant", "invalid or consumed code")
    if _aware(row.expires_at) < _aware(datetime.now(UTC)):
        raise _bad_request("invalid_grant", "code expired")
    if row.client_id != client.client_id or row.redirect_uri != redirect_uri:
        raise _bad_request("invalid_grant", "code binding mismatch")
    if row.code_challenge_method != "S256":
        # Only S256 is ever stored now. A row left over from a release that
        # still accepted `plain` (60 s lifetime) is refused, not compared.
        raise _bad_request("invalid_grant", "pkce method not supported")
    if not _pkce_ok(code_verifier, row.code_challenge):
        raise _bad_request("invalid_grant", "pkce verification failed")
    bound = rules.normalise_resource(row.resource)
    asked = rules.normalise_resource(resource)
    if asked and bound and asked != bound:
        raise _bad_request("invalid_target", "resource differs from the one that was approved")
    if asked and not bound:
        problem = rules.resource_problem(asked, _issuer(request))
        if problem:
            raise _bad_request("invalid_target", problem)
    final_resource = bound or asked

    workspace_id = _bound_workspace(row.code)
    scopes = rules.split_scope(row.scope)
    grant_id, scopes = await _session_grant(
        session, user_id=row.user_id, workspace_id=workspace_id,
        client_name=client.name, scopes=scopes)
    cfg = _jwt_config()
    family = _bind_workspace(secrets.token_hex(16), workspace_id)
    refresh = await _mint_refresh(
        session=session, client_id=client.client_id, user_id=row.user_id,
        scope=" ".join(scopes), family_id=family, resource=final_resource, grant_id=grant_id)
    return _token_body(cfg, client_id=client.client_id, user_id=row.user_id,
                       workspace_id=workspace_id, grant_id=grant_id, scopes=scopes,
                       resource=final_resource, refresh=refresh, family_id=family)


def _token_body(cfg, *, client_id: str, user_id: str, workspace_id: str | None,  # noqa: ANN001
                grant_id: str | None, scopes: list[str], resource: str | None,
                refresh: str, family_id: str | None = None) -> dict[str, Any]:
    from src.mcp_server.auth import issue_token

    access = issue_token(
        cfg, subject=user_id, scopes=scopes, client_id=client_id,
        expires_in=_TOKEN_TTL_SECONDS,
        extra_claims=_workspace_claims(workspace_id, grant_id, family_id), resource=resource)
    body: dict[str, Any] = {
        "access_token": access, "token_type": "Bearer", "expires_in": _TOKEN_TTL_SECONDS,
        "refresh_token": refresh, "scope": " ".join(scopes),
    }
    return body


async def _session_grant(
    session: AsyncSession, *, user_id: str, workspace_id: str | None,
    client_name: str, scopes: list[str],
) -> tuple[str, list[str]]:
    """The ``mcp_tokens`` row a new session runs under, and the scopes it may carry.

    Authority is judged again here, not trusted from the consent that happened
    a minute ago: the account must be active and still in the workspace.

    An administrator's ``oauth_grant`` for the person is used when there is one
    (its repository list and write switch are the ceiling). Otherwise a ``self``
    row is made for THIS connection: the person's own access is the ceiling and
    revoking the connection revokes the row. That happens only where the install
    allows people to connect themselves (``CELMIS_MCP_SELF_SERVICE``) or the
    person may issue tokens anyway.
    """
    if not workspace_id:
        raise _bad_request("invalid_grant", "this code names no workspace")
    if not _account_is_usable(user_id):
        raise _bad_request("invalid_grant", "access was withdrawn")
    if not await _still_member(session, user_id, workspace_id):
        raise _bad_request("invalid_grant", "access was withdrawn")

    from src.mcp_server import token_store

    admin_grant = await _grant_id(session, user_id, workspace_id)
    if admin_grant:
        view = await session.run_sync(lambda s: token_store.lookup_fresh(s, admin_grant))
        ceiling = set(view.scopes) if view is not None else set()
        kept = [s for s in scopes if s in ceiling or s == "read:code"]
        return admin_grant, kept or [s for s in scopes if s.startswith("read:")]

    from src.users import get_user_store

    user = get_user_store().get_by_id(user_id)
    if not (token_store.self_service_enabled() or (user is not None and token_store.may_issue(user))):
        raise _bad_request("invalid_grant", _NO_GRANT)
    writes = [s for s in scopes if s.startswith("write:")]

    def _create(sync_session):  # noqa: ANN001, ANN202
        row = token_store.create_row(
            sync_session, kind="self", workspace_id=workspace_id, user_id=user_id,
            issued_by=user_id, label=f"oauth: {client_name}", patterns=["*"],
            allow_write=bool(writes), profile="full",
            expires_in_days=token_store.max_days(), write_scopes=writes)
        return row.id

    return await session.run_sync(_create), scopes


async def _still_member(session: AsyncSession, user_id: str, workspace_id: str) -> bool:
    """Whether the person may still act in the workspace: a member, or a global admin."""
    if await session.get(WorkspaceMember, (workspace_id, user_id)) is not None:
        return True
    try:
        from src.users import get_user_store

        user = get_user_store().get_by_id(user_id)
    except Exception:  # noqa: BLE001 — fail closed
        return False
    return bool(user is not None and user.is_admin)


# ─── Client-credentials grant (service-to-service, Stage 21) ────────


async def _client_credentials_grant(*, client: OAuthClient) -> dict[str, Any]:
    """Machine-to-machine tokens for CI / backend integrations.

    Confidential clients ONLY — a public (PKCE) client has no secret, so
    this grant would be an unauthenticated token mint. Scopes = the
    client's full allowed_scopes (no down-scoping param in the MVP).
    No refresh token is issued (RFC 6749 §4.4.3 — clients just re-auth).
    """
    if not client.client_secret_hash:
        raise _bad_request("unauthorized_client",
                           "client_credentials requires a confidential client")
    from src.mcp_server.auth import issue_token

    cfg = _jwt_config()
    scopes = list(client.allowed_scopes or [])
    token = issue_token(
        cfg, subject=f"client:{client.client_id}",
        scopes=scopes, client_id=client.client_id,
        expires_in=_TOKEN_TTL_SECONDS,
    )
    logger.info("oauth_client_credentials_issued client=%s scopes=%s", client.client_id, scopes)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": _TOKEN_TTL_SECONDS,
        "scope": " ".join(scopes),
    }


# ─── Refresh grant ──────────────────────────────────────────────────


async def _refresh_grant(
    request: Request, *, client: OAuthClient, refresh_token: str | None,
    resource: str | None, session: AsyncSession,
) -> dict[str, Any]:
    if not refresh_token:
        raise _bad_request("invalid_request", "refresh_token required")
    client_id = client.client_id
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    row = (await session.scalars(
        select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == token_hash)
    )).first()
    if row is None:
        raise _bad_request("invalid_grant", "invalid refresh_token")
    if row.client_id != client_id:
        raise _bad_request("invalid_grant", "refresh_token belongs to different client")

    now = _aware(datetime.now(UTC))

    # Detect reuse — the RFC 9700 mitigation. If this token was already
    # rotated (rotated_to != NULL) OR revoked, the whole family is
    # burned. This defeats the class of attacks where an attacker steals
    # a refresh token and both attacker + legit client try to use it.
    if row.rotated_to is not None or row.revoked:
        await _revoke_family(session, row.family_id)
        raise _bad_request("invalid_grant", "refresh token reuse detected — family revoked")

    if _aware(row.expires_at) < now:
        raise _bad_request("invalid_grant", "refresh_token expired")

    asked = rules.normalise_resource(resource)
    if asked and asked != rules.normalise_resource(row.resource):
        raise _bad_request("invalid_target", "resource differs from the one that was approved")

    # An account that was switched off (or deleted) since the last refresh, a
    # person who left the workspace, a grant that was revoked or has expired:
    # the session ends here, in the words of a withdrawn grant, so the answer
    # does not say which it was. (The MCP verifier refuses the token it would
    # have issued at use anyway; this ends the chain a step earlier.)
    workspace_id = _bound_workspace(row.family_id)
    if not await _session_still_good(session, row, workspace_id):
        await _revoke_family(session, row.family_id)
        raise _bad_request("invalid_grant", "access was withdrawn")
    cfg = _jwt_config()

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
        raise _bad_request("invalid_grant", "refresh token reuse detected — family revoked")

    grant_id = row.grant_id or await _grant_id(session, row.user_id, workspace_id)
    new_refresh = await _mint_refresh(
        session=session, client_id=client_id, user_id=row.user_id,
        scope=row.scope, family_id=row.family_id, resource=row.resource, grant_id=row.grant_id)
    # Replace the 'pending' marker with the real new-token hash.
    await session.execute(
        update(OAuthRefreshToken)
        .where(OAuthRefreshToken.token_hash == token_hash)
        .values(rotated_to=hashlib.sha256(new_refresh.encode()).hexdigest())
    )
    await session.commit()
    logger.info("oauth_refresh_rotated client=%s user=%s family=%s",
                client_id, row.user_id, row.family_id)
    return _token_body(cfg, client_id=client_id, user_id=row.user_id,
                       workspace_id=workspace_id, grant_id=grant_id,
                       scopes=rules.split_scope(row.scope), resource=row.resource,
                       refresh=new_refresh, family_id=row.family_id)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _session_still_good(
    session: AsyncSession, row: OAuthRefreshToken, workspace_id: str | None,
) -> bool:
    if not _account_is_usable(row.user_id):
        return False
    if workspace_id and not await _still_member(session, row.user_id, workspace_id):
        return False
    if row.grant_id:
        from src.mcp_server import token_store

        view = await session.run_sync(lambda s: token_store.lookup_fresh(s, row.grant_id))
        return bool(view is not None and view.problem() is None and view.user_id == row.user_id)
    # A session from before grants were recorded on the row: the person's active
    # `oauth_grant` in the workspace, as it always was.
    return (await _grant_id(session, row.user_id, workspace_id)) is not None


async def _mint_refresh(
    *, session: AsyncSession, client_id: str, user_id: str,
    scope: str, family_id: str, resource: str | None = None, grant_id: str | None = None,
) -> str:
    """Insert a refresh token row + return the raw token string. Only
    the hash is persisted."""
    raw = secrets.token_urlsafe(48)
    row = OAuthRefreshToken(
        id=str(uuid.uuid4()),
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        client_id=client_id, user_id=user_id, scope=scope,
        family_id=family_id, resource=resource, grant_id=grant_id, revoked=False,
        expires_at=datetime.now(UTC) + timedelta(seconds=_REFRESH_TTL_SECONDS),
    )
    session.add(row)
    await session.commit()
    return raw


def _account_is_usable(user_id: str) -> bool:
    """True when the person exists and is switched on. A lookup that cannot be
    made says no (fail closed)."""
    try:
        from src.users import get_user_store

        user = get_user_store().get_by_id(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("oauth_user_lookup_failed err=%s", type(exc).__name__)
        return False
    return user is not None and bool(getattr(user, "is_active", True))


async def _revoke_family(session: AsyncSession, family_id: str) -> None:
    rows = (await session.scalars(
        select(OAuthRefreshToken).where(OAuthRefreshToken.family_id == family_id)
    )).all()
    for r in rows:
        r.revoked = True
    await session.commit()
    # A session made for one connection ends with it: its grant row goes too, so
    # the access token still in the client's hands stops working now, not at the
    # end of its hour.
    await _revoke_grants(session, rows, by="oauth")
    logger.warning("oauth_refresh_family_revoked family=%s tokens=%d",
                   family_id, len(rows))


async def _revoke_grants(session: AsyncSession, rows: list[OAuthRefreshToken], *, by: str) -> None:
    """Revoke the per-connection (`self`) grant rows behind these sessions. An
    administrator's `oauth_grant` is shared by every client of that person and is
    never touched here."""
    ids = {r.grant_id for r in rows if r.grant_id}
    if not ids:
        return

    def _do(sync_session):  # noqa: ANN001, ANN202
        from src.db.models import McpToken
        from src.mcp_server import token_store

        for gid in ids:
            grant = sync_session.get(McpToken, gid)
            if grant is not None and grant.kind == "self":
                token_store.revoke(sync_session, gid, by)

    await session.run_sync(_do)


# ─── Connected apps ─────────────────────────────────────────────────


class ConnectionOut(BaseModel):
    id: str
    client_id: str
    client_name: str
    redirect_host: str
    scopes: list[str]
    workspace_id: str | None
    workspace_name: str | None
    person: str | None
    mine: bool
    created_at: str
    last_used_at: str | None
    expires_at: str


async def _administered_workspaces(session: AsyncSession, user: User) -> set[str]:
    from src.users.roles import WORKSPACE_ADMIN_ROLES

    ids = (await session.scalars(
        select(WorkspaceMember.workspace_id).where(
            WorkspaceMember.user_id == user.id,
            WorkspaceMember.role.in_(sorted(WORKSPACE_ADMIN_ROLES))))).all()
    return set(ids)


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> list[ConnectionOut]:
    """The apps signed in through the browser flow: one row per live session.

    A person sees their own. An owner or admin of a workspace also sees the
    ones made in THAT workspace, by anybody; a platform admin sees all."""
    now = _aware(datetime.now(UTC))
    live = (await session.scalars(
        select(OAuthRefreshToken).where(
            OAuthRefreshToken.revoked.is_(False), OAuthRefreshToken.rotated_to.is_(None))
    )).all()
    live = [r for r in live if _aware(r.expires_at) > now]
    admin_of = await _administered_workspaces(session, user)
    visible = [
        r for r in live
        if r.user_id == user.id or user.is_admin or (_bound_workspace(r.family_id) in admin_of)
    ]
    if not visible:
        return []
    firsts = dict((await session.execute(
        select(OAuthRefreshToken.family_id, func.min(OAuthRefreshToken.issued_at))
        .where(OAuthRefreshToken.family_id.in_({r.family_id for r in visible}))
        .group_by(OAuthRefreshToken.family_id))).all())
    clients = {c.client_id: c for c in (await session.scalars(
        select(OAuthClient).where(OAuthClient.client_id.in_({r.client_id for r in visible})))).all()}
    wanted_ws = {w for w in (_bound_workspace(r.family_id) for r in visible) if w}
    names = dict((await session.execute(
        select(Workspace.id, Workspace.name).where(Workspace.id.in_(wanted_ws)))).all()) if wanted_ws else {}

    from src.mcp_server import token_store
    from src.users import get_user_store

    store = get_user_store()
    out: list[ConnectionOut] = []
    for r in visible:
        client = clients.get(r.client_id)
        ws = _bound_workspace(r.family_id)
        used = _aware(r.issued_at)
        if r.grant_id:
            view = await session.run_sync(lambda s, g=r.grant_id: token_store.lookup_fresh(s, g))
            if view is not None and view.last_used_at and view.last_used_at > used:
                used = view.last_used_at
        person = None
        if r.user_id != user.id:
            who = store.get_by_id(r.user_id)
            person = getattr(who, "email", None) or r.user_id
        first = firsts.get(r.family_id) or r.issued_at
        out.append(ConnectionOut(
            id=r.family_id, client_id=r.client_id,
            client_name=client.name if client else r.client_id,
            redirect_host=rules.host_of((client.redirect_uris or [""])[0]) if client else "",
            scopes=rules.split_scope(r.scope), workspace_id=ws, workspace_name=names.get(ws),
            person=person, mine=r.user_id == user.id,
            created_at=_aware(first).isoformat(), last_used_at=used.isoformat(),
            expires_at=_aware(r.expires_at).isoformat()))
    out.sort(key=lambda c: c.last_used_at or "", reverse=True)
    return out


@router.delete("/connections/{connection_id}", status_code=204)
async def revoke_connection(
    connection_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> Response:
    """Revoke one connected app: its refresh tokens, and the grant row its access
    token runs under, so it stops working now and cannot be refreshed."""
    rows = (await session.scalars(
        select(OAuthRefreshToken).where(OAuthRefreshToken.family_id == connection_id)
    )).all()
    if not rows:
        raise HTTPException(status_code=404, detail="no such connection")
    ws = _bound_workspace(connection_id)
    allowed = (rows[0].user_id == user.id or user.is_admin
               or (ws is not None and ws in await _administered_workspaces(session, user)))
    if not allowed:
        # Same answer as a missing one: a family id is not something to probe.
        raise HTTPException(status_code=404, detail="no such connection")
    for r in rows:
        r.revoked = True
    await session.commit()
    await _revoke_grants(session, rows, by=user.id)
    logger.info("oauth_connection_revoked family=%s by=%s", connection_id, user.email)
    return Response(status_code=204)


__all__ = ["OAuthRefusal", "oauth_refusal_handler", "router"]
