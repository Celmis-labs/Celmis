"""Auth routes — login, signup, Google OAuth callback, /me."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.api.deps import client_ip, get_current_user, get_users
from src.api.jwt_auth import issue_token
from src.api.schemas import (
    ForgotPasswordRequest,
    GoogleCallbackRequest,
    LoginRequest,
    ResetPasswordRequest,
    SignupRequest,
    TokenResponse,
    UserOut,
)
from src.http import build_client
from src.security.audit import record_action
from src.users import (
    User,
    UserAuthMethod,
    UserExistsError,
    UserStore,
    hash_password,
    verify_password,
)
from src.users.scopes import STANDARD_SCOPES, held_scopes

logger = logging.getLogger(__name__)




router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_to_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        is_admin=user.is_admin,
        auth_method=user.auth_method.value,
        has_password=user.has_password,
        has_google=user.has_google,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


_MASTER_ADMIN_ID = "master-admin"


def _master_email() -> str:
    """No in-code default: the master identity exists ONLY when the operator
    explicitly sets CELMIS_MASTER_EMAIL in the env (alongside the key)."""
    return os.environ.get("CELMIS_MASTER_EMAIL", "").strip().lower()


def _master_login(req, users: UserStore,
                  request: Request | None = None) -> TokenResponse | None:
    """LiteLLM-style bootstrap superadmin: CELMIS_MASTER_KEY in env + the
    master email as login. This is the ONLY way to obtain global-admin
    rights — a recovery/ops account controlled by whoever runs the box.

    Returns a TokenResponse on success, None to fall through to the normal
    login path. Fail-closed: no env key → the path does not exist."""
    import hmac

    key = os.environ.get("CELMIS_MASTER_KEY", "").strip()
    if not key or len(key) < 12:
        # Refuse trivially short keys outright — a 4-char master password
        # would be a bigger hole than no master account at all.
        return None
    # Length was not enough. `.env.example` shipped
    # `CELMIS_MASTER_KEY=   # openssl rand -hex 24`, and a dotenv parser reads
    # everything after `=` as the value — so an install that copied the file
    # verbatim had a 22-character master password printed in this repository,
    # long enough to clear the check above. The same gate the JWT secret uses
    # refuses it, and refuses the whole class: a password with a space in it
    # is an instruction somebody forgot to replace.
    from src.api.jwt_auth import secret_problem

    problem = secret_problem(key, check_length=False)
    if problem:
        logger.error(
            "master_key_unusable — CELMIS_MASTER_KEY %s; the master login is "
            "disabled until it is set to a real secret", problem,
        )
        return None
    master_email = _master_email()
    if not master_email:
        return None  # email not set in env → the path is disabled
    if req.email.strip().lower() != master_email:
        return None
    if not hmac.compare_digest(req.password.encode(), key.encode()):
        # Somebody who knows the master email and guessed at the key. The
        # single highest-signal failure this product can record, and it was
        # indistinguishable from a mistyped password on an ordinary account.
        record_action(
            action="auth.master_login_failed", actor=master_email,
            target="master-key", ip=client_ip(request) if request else None,
            error="wrong master key",
        )
        return None

    # BY ID FIRST, and this ordering is a fix rather than a tidy-up.
    #
    # The id is fixed (`master-admin`); the email is an env var an operator can
    # change. Looking up only by email meant that after such a change the
    # lookup found nothing, `create` hit the unique id, and the master login
    # answered 500 — FOREVER, on every subsequent attempt. This is the account
    # documented four lines above as "the ONLY way to obtain global-admin
    # rights — a recovery/ops account". The recovery path destroyed itself the
    # first time somebody edited the address it answers to.
    #
    # Reproduced by pointing CELMIS_MASTER_EMAIL at a second address:
    #     UserExistsError: id 'master-admin' already exists
    user = users.get_by_id(_MASTER_ADMIN_ID) or users.get_by_email(master_email)
    if user is None:
        user = User(
            id=_MASTER_ADMIN_ID,
            email=master_email,
            name="Master Admin",
            auth_method=UserAuthMethod.PASSWORD,
            password_hash=None,  # login only via master key, not via the DB
            is_admin=True,
            scopes=list(STANDARD_SCOPES),
        )
        users.create(user)
    else:
        # Re-assert on every master login: the account cannot be demoted or
        # deactivated out of its recovery role, and it answers to whatever
        # address the env now names.
        changed = False
        if user.email != master_email:
            logger.warning("master_admin_email_changed from=%s to=%s",
                           user.email, master_email)
            user.email = master_email
            changed = True
        if not user.is_admin or not user.is_active:
            user.is_admin = True
            user.is_active = True
            changed = True
        if changed:
            try:
                users.update(user)
            except Exception as exc:  # noqa: BLE001
                # Somebody else already holds the new address. Refuse rather
                # than 500: the operator has a name collision to resolve, and
                # a stack trace does not tell them that.
                logger.error(
                    "master_admin_email_taken email=%s err=%s — set "
                    "CELMIS_MASTER_EMAIL to an address no other account uses",
                    master_email, exc)
                return None
    users.update_last_login(user.id)
    token, exp = issue_token(user_id=user.id, email=user.email, scopes=held_scopes(user))
    logger.warning("master_admin_login")  # always visible in the logs
    # The most privileged act available anywhere in the product: it grants
    # global admin, re-asserts it over any demotion, and is the only path that
    # can. It had a log line and no audit row — so the one action an
    # investigation starts from was absent from the file an investigation
    # reads.
    record_action(
        action="auth.master_login", actor=user.email, actor_id=user.id,
        target="master-key",
        ip=client_ip(request) if request else None,
        detail={"granted": "global-admin"},
    )
    return TokenResponse(access_token=token, expires_at=exp)


def _record_login_failure(email: str, request: Request | None, reason: str,
                          *, actor_id: str | None = None) -> None:
    """A refused sign-in, in the file an auditor reads.

    Only successes were recorded. "Who logged in" without "who tried and
    failed" cannot answer the question the log exists for — a brute-force run
    leaves no trace at all until it succeeds, and then leaves exactly one row
    indistinguishable from an ordinary Tuesday.

    The attempted address goes in as the actor. It is attacker-controlled text
    and that is the point: it is the only identity a failed attempt has. The
    password never appears, not even its length.
    """
    record_action(
        action="auth.login_failed",
        actor=(email or "").strip()[:200] or "(no email)",
        actor_id=actor_id,
        target="password",
        ip=client_ip(request) if request else None,
        error=reason,
    )


@router.post("/signup", response_model=TokenResponse)
def signup(req: SignupRequest, request: Request,
           users: UserStore = Depends(get_users)) -> TokenResponse:
    existing = users.get_by_email(req.email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered",
        )
    user = User(
        id=str(uuid.uuid4()),
        email=req.email,
        name=req.name or req.email.split("@", 1)[0],
        auth_method=UserAuthMethod.PASSWORD,
        password_hash=hash_password(req.password),
        # We do NOT hand out global admin on signup — the only way is the
        # master account with CELMIS_MASTER_KEY (see _master_login).
        is_admin=False,
        scopes=list(STANDARD_SCOPES),
    )
    try:
        users.create(user)
    except UserExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    users.update_last_login(user.id)
    token, exp = issue_token(user_id=user.id, email=user.email, scopes=held_scopes(user))
    logger.info("user_signup id=%s email=%s admin=%s", user.id, user.email, user.is_admin)
    record_action(
        action="auth.signup", actor=user.email, actor_id=user.id,
        target="password", ip=client_ip(request),
        detail={"auth_method": "password"},
    )
    from src.api.workspace_provision import provision_personal_workspace
    provision_personal_workspace(user.id, user.email, user.name)
    return TokenResponse(access_token=token, expires_at=exp)


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request,
          users: UserStore = Depends(get_users)) -> TokenResponse:
    master = _master_login(req, users, request)
    if master is not None:
        return master
    user = users.get_by_email(req.email)
    if user is None or not user.is_active:
        # Recorded, and the two cases kept apart. The RESPONSE stays a single
        # "Invalid credentials" — telling a stranger which half was wrong is
        # an account-enumeration oracle — but the audit file is read by the
        # operator, who needs the difference: a run of `unknown-account`
        # against many addresses is credential stuffing, a run of
        # `wrong-password` against one is a targeted guess.
        _record_login_failure(
            req.email, request,
            "inactive-account" if user is not None else "unknown-account")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials",
        )
    if not user.has_password or user.password_hash is None:
        _record_login_failure(req.email, request, "password-login-not-enabled",
                              actor_id=user.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account uses Google sign-in only",
        )
    if not verify_password(req.password, user.password_hash):
        _record_login_failure(req.email, request, "wrong-password",
                              actor_id=user.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials",
        )
    users.update_last_login(user.id)
    token, exp = issue_token(user_id=user.id, email=user.email, scopes=held_scopes(user))
    logger.info("user_login id=%s email=%s", user.id, user.email)
    # The one action every auditor asks about first, and the file had no row
    # for it. No password, no token, no session id — who, when, from where.
    # No workspace: login happens before one is chosen, and resolving it here
    # would put a Postgres round trip in the hot path of every sign-in. The
    # READER handles it instead — see `_Scope.allows`, which lets a caller see
    # their own actions whatever tenant the row carries.
    record_action(
        action="auth.login", actor=user.email, actor_id=user.id,
        target="password",
        ip=client_ip(request),
    )
    from src.api.workspace_provision import provision_personal_workspace
    provision_personal_workspace(user.id, user.email, user.name)
    return TokenResponse(access_token=token, expires_at=exp)


@router.post("/google", response_model=TokenResponse)
def google_callback(
    req: GoogleCallbackRequest, request: Request,
    users: UserStore = Depends(get_users),
) -> TokenResponse:
    """Verify Google ID token, create or link user, return JWT.

    Frontend (NextAuth) handles the OAuth dance and forwards the ID token.
    We verify it here against Google's tokeninfo endpoint (simple, no SDK).
    """
    expected_aud = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    if not expected_aud:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GOOGLE_OAUTH_CLIENT_ID not configured on server",
        )

    try:
        with build_client(timeout=10.0) as client:
            resp = client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": req.id_token},
            )
        resp.raise_for_status()
        claims = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("google_tokeninfo_failed err=%s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google ID token",
        ) from exc

    if claims.get("aud") != expected_aud:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token audience mismatch",
        )
    if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token issuer",
        )

    sub = claims.get("sub")
    email = claims.get("email")
    name = claims.get("name") or ""
    if not sub or not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing required claims",
        )

    user = users.get_by_google_sub(sub) or users.get_by_email(email)
    if user is None:
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            name=name,
            auth_method=UserAuthMethod.GOOGLE_OAUTH,
            google_sub=sub,
            is_admin=False,
            scopes=list(STANDARD_SCOPES),
        )
        users.create(user)
    elif not user.has_google:
        user.google_sub = sub
        user.auth_method = (
            UserAuthMethod.BOTH if user.has_password else UserAuthMethod.GOOGLE_OAUTH
        )
        users.update(user)

    users.update_last_login(user.id)
    token, exp = issue_token(user_id=user.id, email=user.email, scopes=held_scopes(user))
    logger.info("user_google_login id=%s email=%s", user.id, user.email)
    # Same action as the password path, different `target`. Two actions would
    # mean every query for "who signed in" has to know there are two, and the
    # one that gets forgotten is the one an attacker uses.
    record_action(
        action="auth.login", actor=user.email, actor_id=user.id,
        target="google", ip=client_ip(request),
    )
    from src.api.workspace_provision import provision_personal_workspace
    provision_personal_workspace(user.id, user.email, user.name)
    return TokenResponse(access_token=token, expires_at=exp)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_to_out(user)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    request: Request,
    user: User = Depends(get_current_user),
    users: UserStore = Depends(get_users),
) -> None:
    """Permanently delete the caller's OWN account.

    Revokes refresh tokens, drops every workspace membership, deletes the user's
    personal workspace and its per-workspace credential rows, then removes the
    user record. Self-service — the caller can only delete themselves. Other
    workspaces the user happened to own are left intact (may be re-assigned).
    """
    from sqlalchemy import delete as _delete
    from sqlalchemy import select as _select
    from sqlalchemy import update as _update
    from sqlalchemy.orm import Session

    from src.api.workspace_provision import personal_slug
    from src.db.models import OAuthRefreshToken, Workspace, WorkspaceMember
    from src.llm.budget import _engine

    with Session(_engine()) as s:
        s.execute(
            _update(OAuthRefreshToken)
            .where(OAuthRefreshToken.user_id == user.id, OAuthRefreshToken.revoked.is_(False))
            .values(revoked=True)
        )
        s.execute(_delete(WorkspaceMember).where(WorkspaceMember.user_id == user.id))
        ws = s.execute(
            _select(Workspace).where(Workspace.slug == personal_slug(user.id))
        ).scalar_one_or_none()
        personal_ws_id = ws.id if ws is not None else None
        if ws is not None:
            s.delete(ws)
        s.commit()

    # Best-effort: purge the personal workspace's encrypted credential rows.
    if personal_ws_id is not None:
        try:
            from src.credentials import get_credential_store
            from src.llm.keys import workspace_slot
            store = get_credential_store()
            slot = workspace_slot(personal_ws_id)
            for row in store.list(user_id=slot):
                store.delete(
                    provider=row["provider"], user_id=slot,
                    account_label=str(row.get("account_label", "default")),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("account_delete_cred_purge_failed user=%s err=%s", user.id, exc)

    # BEFORE the row is gone, because the row is where the id and the email
    # come from. An audit written after a delete has to reconstruct who it was
    # talking about, and the record of an account's destruction is exactly the
    # one that must not depend on the account.
    record_action(
        action="auth.account_deleted", actor=user.email, actor_id=user.id,
        workspace_id=personal_ws_id, target=user.email, ip=client_ip(request),
        detail={"personal_workspace": personal_ws_id or ""},
    )
    users.delete(user.id)
    logger.info("account_deleted id=%s email=%s", user.id, user.email)


@router.post("/refresh", response_model=TokenResponse)
def refresh(user: User = Depends(get_current_user)) -> TokenResponse:
    """Re-issue a session token for a still-valid session (Stage 21).

    The NextAuth jwt callback calls this when `celmisExpiresAt` is
    close, so browser sessions silently roll over instead of forcing a
    re-login. Requires the CURRENT token to still be valid — an expired
    token cannot self-refresh (that's what the login flow is for).
    """
    token, exp = issue_token(user_id=user.id, email=user.email, scopes=held_scopes(user))
    logger.info("session_token_refreshed id=%s email=%s", user.id, user.email)
    return TokenResponse(access_token=token, expires_at=exp)


# ─── Password reset (Stage 23) ───────────────────────────────────────
#
# Deliberately does NOT reveal whether an address is registered: /forgot
# always answers 200 with the same body. Only the SHA-256 hash of the token is
# stored, and the raw token never leaves the server through this unauthenticated
# endpoint — see the delivery note in `forgot_password`.

_RESET_TTL_MINUTES = 15


def _hash_token(raw: str) -> str:
    import hashlib
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_reset_token(user_id: str) -> tuple[str, datetime]:
    """Mint a single-use reset token. Returns (raw token, expiry). Only the
    SHA-256 hash is stored, so a database dump does not yield usable links."""
    import secrets
    import uuid as _uuid
    from datetime import timedelta

    from sqlalchemy.orm import Session

    from src.db.models import PasswordResetToken
    from src.llm.budget import _engine  # reuse the sync engine helper

    raw = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(minutes=_RESET_TTL_MINUTES)
    with Session(_engine()) as s:
        s.add(PasswordResetToken(
            id=str(_uuid.uuid4()),
            token_hash=_hash_token(raw),
            user_id=user_id,
            expires_at=expires_at,
        ))
        s.commit()
    return raw, expires_at


@router.post("/forgot-password")
async def forgot_password(
    req: ForgotPasswordRequest,
    users: UserStore = Depends(get_users),
) -> dict:
    user = users.get_by_email(str(req.email))
    out: dict = {"ok": True, "detail": "If that address exists, a reset link was created."}
    if user is None or not user.is_active:
        return out
    if user.id == _MASTER_ADMIN_ID or user.email == _master_email():
        # The master account authenticates ONLY via CELMIS_MASTER_KEY. A
        # reset link would let whoever controls the (fictional) mail domain
        # set a DB password on it — closing that path keeps the env key the
        # single way in.
        return out

    try:
        raw, _ = issue_reset_token(user.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reset_token_create_failed err=%s", exc)
        return out

    logger.info("password_reset_requested user=%s", user.email)
    # The token is NEVER returned here. This endpoint is unauthenticated, so
    # returning it would mean "email address == account takeover" (CWE-640).
    # Preferred channel: email, when SMTP is configured (best-effort, in the
    # background so response timing doesn't leak whether the account exists).
    try:
        from src.notifications.mailer import absolute_url, mailer_configured, send_email_background
        if mailer_configured():
            send_email_background(
                user.email,
                "Celmis password reset",
                "Someone requested a password reset for this account.\n\n"
                f"Reset link (valid 15 minutes):\n{absolute_url(_reset_url(raw))}\n\n"
                "If this wasn't you, ignore this email.",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("reset_email_failed err=%s", exc)
    # With no mailer configured there are two out-of-band channels instead:
    # an admin issues the link (POST /api/users/{id}/reset-link), or the
    # operator reads it from the server log after opting in explicitly.
    if os.environ.get("AUTH_RESET_LINK_TO_LOG", "").strip().lower() in ("1", "true", "yes"):
        logger.warning(
            "password_reset_link user=%s url=%s  ← this link grants access to "
            "the account; treat it as a password",
            user.email, _reset_url(raw),
        )
    return out


def _reset_url(raw: str) -> str:
    """Relative link, same idiom as workspace invites (`/invite/{raw}`) — the
    web app resolves it against its own origin, so no base URL to misconfigure."""
    return f"/reset-password?token={raw}"


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    req: ResetPasswordRequest,
    users: UserStore = Depends(get_users),
) -> None:
    """Set a new password from a reset link.

    Deliberately does *not* return a session. Whoever holds the link proves
    they can set the password, not that they are the account owner — making
    them log in afterwards means a leaked link is worth one password change
    instead of an immediate authenticated session. Existing refresh tokens
    are revoked for the same reason: a reset must evict an intruder who is
    already signed in, not run alongside them.
    """
    from sqlalchemy import select as _select
    from sqlalchemy import update as _update
    from sqlalchemy.orm import Session

    from src.db.models import PasswordResetToken
    from src.llm.budget import _engine

    now = datetime.now(UTC)
    with Session(_engine()) as s:
        row = s.execute(
            _select(PasswordResetToken).where(
                PasswordResetToken.token_hash == _hash_token(req.token)
            )
        ).scalar_one_or_none()
        if row is None or row.used_at is not None or row.expires_at <= now:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        user = users.get_by_id(row.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")

        user.password_hash = hash_password(req.password)
        if user.auth_method == UserAuthMethod.GOOGLE_OAUTH:
            user.auth_method = UserAuthMethod.BOTH
        elif user.auth_method != UserAuthMethod.BOTH:
            user.auth_method = UserAuthMethod.PASSWORD
        users.update(user)

        row.used_at = now

        from src.db.models import OAuthRefreshToken
        revoked = s.execute(
            _update(OAuthRefreshToken)
            .where(OAuthRefreshToken.user_id == user.id, OAuthRefreshToken.revoked.is_(False))
            .values(revoked=True)
        ).rowcount
        s.commit()

    logger.info(
        "password_reset_completed user=%s refresh_tokens_revoked=%s", user.email, revoked,
    )
