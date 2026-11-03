"""The web app's refresh credential: what lets the access token be short.

The browser holds a Bearer token that page scripts can read (the web app hands
it to its own fetch calls). A 30-day token there means one XSS is a month of
access. So the access token is short (``CELMIS_ACCESS_TOKEN_TTL_SECONDS``,
default one hour) and is renewed with a SECOND credential that the browser
never sees: the web server keeps it inside its encrypted session cookie and
presents it to :func:`redeem` from the server side.

Storage reuses ``oauth_refresh_tokens`` (client ``celmis-web``): only the
SHA-256 of the secret is stored, a password reset or an account deletion
already revokes every row of a user, and no migration is needed.

Unlike the MCP refresh tokens the credential is NOT rotated on use. The
NextAuth ``jwt`` callback runs concurrently (middleware, page, session
polling); rotation with reuse detection would read an ordinary race as theft
and sign the person out. Revocation is the control: sign-out calls
:func:`revoke`, and a stolen access token then dies at its own short expiry.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

WEB_CLIENT_ID = "celmis-web"

#: Floor and ceiling of the access token's life. One hour is the ceiling the
#: audit asked for; the floor keeps a typo from signing everybody out every
#: few seconds.
ACCESS_TTL_MIN_SECONDS = 60
ACCESS_TTL_MAX_SECONDS = 3600
ACCESS_TTL_DEFAULT_SECONDS = 3600

SESSION_HOURS_DEFAULT = 24


def access_ttl_seconds() -> int:
    raw = os.environ.get("CELMIS_ACCESS_TOKEN_TTL_SECONDS", "").strip()
    try:
        value = int(raw) if raw else ACCESS_TTL_DEFAULT_SECONDS
    except ValueError:
        value = ACCESS_TTL_DEFAULT_SECONDS
    return max(ACCESS_TTL_MIN_SECONDS, min(ACCESS_TTL_MAX_SECONDS, value))


def session_lifetime() -> timedelta:
    raw = os.environ.get("CELMIS_WEB_SESSION_HOURS", "").strip()
    try:
        hours = float(raw) if raw else SESSION_HOURS_DEFAULT
    except ValueError:
        hours = SESSION_HOURS_DEFAULT
    # Never longer than a day: that is the number the session is held to.
    return timedelta(hours=max(0.1, min(float(SESSION_HOURS_DEFAULT), hours)))


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _engine():
    from src.llm.budget import _engine as engine

    return engine()


def issue(user_id: str, *, sliding: bool = True) -> str:
    """Create a refresh credential for ``user_id`` and return the raw secret.

    ``sliding=False`` marks a single-sign-on-only user: the credential ends
    ``session_lifetime()`` after sign-in whatever happens, so the identity
    provider is asked again at least that often.
    """
    from src.db.models import OAuthRefreshToken

    raw = secrets.token_urlsafe(32)
    row_id = uuid.uuid4().hex
    with Session(_engine()) as s:
        s.add(OAuthRefreshToken(
            id=row_id,
            token_hash=_hash(raw),
            client_id=WEB_CLIENT_ID,
            user_id=user_id,
            scope="web" if sliding else "web:fixed",
            family_id=row_id,
            expires_at=datetime.now(UTC) + session_lifetime(),
        ))
        s.commit()
    return raw


def redeem(raw: str) -> str | None:
    """The user id behind a live credential, or ``None``. Slides its expiry."""
    found = redeem_with_mode(raw)
    return found[0] if found else None


def redeem_with_mode(raw: str) -> tuple[str, bool] | None:
    """``(user id, sliding)`` for a live credential, or ``None``.

    A sliding credential has just had its expiry pushed out; a fixed one
    (single-sign-on-only, see :func:`issue`) keeps the expiry it was born with.
    """
    from src.db.models import OAuthRefreshToken

    if not raw:
        return None
    now = datetime.now(UTC)
    with Session(_engine()) as s:
        row = s.execute(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_hash == _hash(raw),
                OAuthRefreshToken.client_id == WEB_CLIENT_ID,
            )
        ).scalar_one_or_none()
        if row is None or row.revoked:
            return None
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= now:
            return None
        sliding = row.scope == "web"
        if sliding:
            row.expires_at = now + session_lifetime()
            s.commit()
        return row.user_id, sliding


def revoke(raw: str) -> bool:
    """End a credential (sign-out). True when one was found."""
    from src.db.models import OAuthRefreshToken

    if not raw:
        return False
    with Session(_engine()) as s:
        res = s.execute(
            update(OAuthRefreshToken)
            .where(
                OAuthRefreshToken.token_hash == _hash(raw),
                OAuthRefreshToken.client_id == WEB_CLIENT_ID,
            )
            .values(revoked=True)
        )
        s.commit()
        return bool(res.rowcount)
