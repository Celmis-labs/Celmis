# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""``POST /api/auth/oidc`` — company single sign-on (Keycloak / any OIDC IdP).

Mounted by ``src.ee.mount_enterprise`` only when the licence grants ``sso``.
Without it the path does not exist (404), ``/api/capabilities`` reports
``sso`` unavailable, and the login page does not offer the button.

The path stays under ``/api/auth`` so a web build and an API build of the same
release agree on it whichever edition each is running.

What this module owns is the exchange: verify the id_token, find or link the
account, sync the IdP admin role. What it deliberately does NOT own, because
it is not enterprise and must not disappear with a licence:

  * the ``oidc_iss`` / ``oidc_sub`` columns and ``UserAuthMethod.OIDC``
    (src/users) — an SSO user created under a licence must stay readable
    after it lapses;
  * ``_is_master_account`` (src/api/routers/auth.py) — the break-glass guard
    is a security control and is imported from the AGPL side;
  * the refusal to renew an SSO-only session (``/api/auth/refresh``).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from src.api.deps import client_ip, get_users
from src.api.jwt_auth import issue_token
from src.api.routers.auth import _MASTER_ADMIN_ID, _is_master_account
from src.api.schemas import OidcCallbackRequest, TokenResponse
from src.ee.sso import oidc
from src.security.audit import record_action
from src.users import User, UserAuthMethod, UserExistsError, UserStore
from src.users.scopes import STANDARD_SCOPES, held_scopes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth", "enterprise"])


@router.post("/oidc", response_model=TokenResponse)
def oidc_callback(
    req: OidcCallbackRequest, request: Request,
    users: UserStore = Depends(get_users),
) -> TokenResponse:
    """Verify an OIDC (Keycloak) id_token, create or link the user, return JWT.

    Same shape as `/google`, with two differences that matter:

      * the token is verified locally against the issuer's JWKS
        (src/ee/sso/oidc.py) — iss, aud (+azp), typ, exp and the signature;
      * an unknown (iss, sub) is accepted ONLY when the IdP says the email
        is verified, both for linking an existing account and for creating
        a new one. Otherwise anyone who can register an unverified address
        at the IdP could sign in as that account, or claim the address
        before its owner does (invites resolve accounts by email).

    The user is found by (iss, sub) first; the email is only used to link
    an account that has no OIDC subject yet.
    """
    config = oidc.oidc_config()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC sign-in is not configured on server",
        )
    try:
        claims = oidc.verify_id_token(req.id_token, config)
    except oidc.OidcError as exc:
        logger.warning("oidc_token_rejected err=%s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid OIDC ID token",
        ) from exc

    sub = str(claims.get("sub") or "")
    email = str(claims.get("email") or "").strip()
    name = str(claims.get("name") or claims.get("preferred_username") or "")
    if not sub or not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing required claims",
        )
    verified = oidc.email_is_verified(claims)

    user = users.get_by_oidc(config.issuer, sub)
    if user is None:
        existing = users.get_by_email(email)
        if existing is not None and _is_master_account(existing):
            # The master account signs in ONLY with CELMIS_MASTER_KEY. An IdP
            # user that happens to carry the master email must not inherit it.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This account cannot use single sign-on",
            )
        if existing is not None and not existing.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled",
            )
        if not verified:
            # Refused for a new account too, not only for linking. Celmis
            # treats a stored email as identity: an email invite adds an
            # existing account straight to the workspace, and a later Google
            # sign-in links to it by email. An unverified IdP address would
            # get both under someone else's name.
            record_action(
                action="auth.login_failed", actor=email[:200],
                actor_id=existing.id if existing is not None else None,
                target="oidc", ip=client_ip(request),
                error="unverified-email-link" if existing is not None
                else "unverified-email-signup",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The identity provider has not verified this email address",
            )
        if existing is not None:
            if existing.has_oidc:
                # Already bound to a different subject (or issuer). Rebinding
                # silently would let a second IdP account take this one over.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Account is linked to a different single sign-on identity",
                )
            existing.oidc_iss = config.issuer
            existing.oidc_sub = sub
            users.update(existing)
            user = existing
        else:
            user = User(
                id=str(uuid.uuid4()),
                email=email,
                name=name or email.split("@", 1)[0],
                auth_method=UserAuthMethod.OIDC,
                oidc_iss=config.issuer,
                oidc_sub=sub,
                is_admin=False,
                scopes=list(STANDARD_SCOPES),
            )
            try:
                users.create(user)
            except UserExistsError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(exc),
                ) from exc

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled",
        )

    # Optional IdP-managed admin flag. Granting is re-applied on every login;
    # revoking only with OIDC_ADMIN_ROLE_SYNC=true, so an admin promoted by
    # hand (CLI) is not demoted the first time they use SSO.
    if config.admin_role:
        has_role = config.admin_role in oidc.token_roles(claims)
        if has_role and not user.is_admin:
            user.is_admin = True
            users.update(user)
            logger.info("oidc_admin_granted id=%s email=%s", user.id, user.email)
        elif (not has_role and user.is_admin and config.admin_role_sync
              and user.id != _MASTER_ADMIN_ID):
            # Never the master account: it is the break-glass identity, and
            # an IdP that dropped a role must not be able to lock it out.
            user.is_admin = False
            users.update(user)
            logger.info("oidc_admin_revoked id=%s email=%s", user.id, user.email)

    users.update_last_login(user.id)
    token, exp = issue_token(user_id=user.id, email=user.email, scopes=held_scopes(user))
    logger.info("user_oidc_login id=%s email=%s", user.id, user.email)
    record_action(
        action="auth.login", actor=user.email, actor_id=user.id,
        target="oidc", ip=client_ip(request),
    )
    from src.api.workspace_provision import provision_personal_workspace
    provision_personal_workspace(user.id, user.email, user.name)
    return TokenResponse(access_token=token, expires_at=exp)
