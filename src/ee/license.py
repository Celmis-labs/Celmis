# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The enterprise licence: an offline, signed token, checked at start-up.

WHAT A LICENCE IS
-----------------
A JWT signed with Ed25519 (``alg: EdDSA``) by a private key that never enters
this repository. Its claims:

    iss       "celmis-licensing" — nothing else is a Celmis licence
    sub       the customer, as it is shown to an administrator
    features  the enterprise features it grants, e.g. ["sso", "analytics"]
    iat, exp  required; nbf optional

It is verified here, against :data:`PUBLIC_KEY_PEM`, and nowhere else. There
is no call home and no revocation list: self-hosting has to work in a closed
network, so the only facts a licence can carry are the ones signed into it.

WHERE IT COMES FROM
-------------------
``CELMIS_LICENSE_KEY`` (the token itself) or ``CELMIS_LICENSE_FILE`` (a path
to a file holding it). The variable wins when both are set — it is the one a
container orchestrator injects, and a stale file left in a volume must not
outrank it.

WHAT IT IS NOT
--------------
Not an authorisation boundary. A licence decides which routers are MOUNTED
(see ``src/ee/__init__.py``); every endpoint behind it still does its own
401/403. It is also not secret: the token names a customer and some features,
so it is kept out of the logs anyway, but holding one grants nothing a
signature check did not.

THE FAILURE DIRECTION
---------------------
Anything that is not a valid licence — absent, unreadable, wrong signature,
wrong algorithm, wrong issuer, expired, not yet valid, no features — is the
community edition. The process starts either way: an expired licence must not
take an installation down, it must take the enterprise features away and say
so in the log.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

logger = logging.getLogger(__name__)

#: The licensing public key. The matching private key is held by the owner,
#: outside the repository. Replacing this constant is the only way to accept
#: a licence somebody else signed — which is exactly what LICENSE_EE §2
#: forbids, and what tests do with a throwaway keypair.
PUBLIC_KEY_PEM = """\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAu7TmMc2m1o3FT2UEXgZoQ6c+DE9gInyHyp0xDEfsYbs=
-----END PUBLIC KEY-----
"""

ISSUER = "celmis-licensing"
ALGORITHM = "EdDSA"

#: Enterprise features this build knows how to mount. A licence may name
#: others (a newer licence on an older build); they are ignored, not refused.
KNOWN_FEATURES: tuple[str, ...] = ("sso", "analytics")

ENV_KEY = "CELMIS_LICENSE_KEY"
ENV_FILE = "CELMIS_LICENSE_FILE"

#: Clock skew tolerated on exp / nbf / iat.
LEEWAY_SECONDS = 60
#: A licence token is a few hundred bytes. Anything far larger is not one,
#: and is not worth handing to a parser.
_MAX_TOKEN_BYTES = 16 * 1024


class LicenseError(Exception):
    """The token is not a valid Celmis licence. The message never contains it."""


@dataclass(frozen=True)
class License:
    customer: str
    features: frozenset[str]
    issued_at: datetime
    expires_at: datetime
    not_before: datetime | None = None

    def grants(self, feature: str) -> bool:
        return feature in self.features

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at

    def summary(self) -> dict[str, Any]:
        """What the rest of the process may know: never the token."""
        return {
            "edition": "enterprise",
            "customer": self.customer,
            "expires_at": self.expires_at.isoformat(),
            "features": sorted(self.features),
        }


def _public_key() -> Ed25519PublicKey:
    key = load_pem_public_key(PUBLIC_KEY_PEM.encode("ascii"))
    if not isinstance(key, Ed25519PublicKey):  # pragma: no cover — a bad edit
        raise LicenseError("the embedded licensing key is not an Ed25519 key")
    return key


def _ts(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def verify(token: str) -> License:
    """Check `token` and return the licence it carries, or raise LicenseError.

    The algorithm is pinned to EdDSA before anything else is read, so an
    ``alg: none`` or an HS256 token "signed" with the public key is refused
    without being tried.
    """
    token = (token or "").strip()
    if not token:
        raise LicenseError("empty")
    if len(token.encode("utf-8", "replace")) > _MAX_TOKEN_BYTES:
        raise LicenseError("too large to be a licence")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise LicenseError("not a JWT") from exc
    if header.get("alg") != ALGORITHM:
        raise LicenseError(f"algorithm {header.get('alg')!r} is not accepted")

    options: dict[str, Any] = {"require": ["exp", "iat", "iss", "sub"]}
    try:
        claims = jwt.decode(
            token, _public_key(), algorithms=[ALGORITHM], issuer=ISSUER,
            leeway=LEEWAY_SECONDS, options=options,
        )
    except jwt.ExpiredSignatureError as exc:
        raise LicenseError("expired") from exc
    except jwt.ImmatureSignatureError as exc:
        raise LicenseError("not valid yet") from exc
    except jwt.InvalidIssuerError as exc:
        raise LicenseError("wrong issuer") from exc
    except jwt.InvalidSignatureError as exc:
        raise LicenseError("signature does not verify") from exc
    except jwt.PyJWTError as exc:
        raise LicenseError(f"invalid ({type(exc).__name__})") from exc

    try:
        issued_at = _ts(claims["iat"])
        expires_at = _ts(claims["exp"])
        not_before = _ts(claims["nbf"]) if "nbf" in claims else None
    except (TypeError, ValueError, OverflowError) as exc:
        raise LicenseError("malformed time claim") from exc

    customer = claims.get("sub")
    if not isinstance(customer, str) or not customer.strip():
        raise LicenseError("no customer")
    raw = claims.get("features")
    if not isinstance(raw, list) or not all(isinstance(f, str) for f in raw):
        raise LicenseError("features must be a list of names")
    unknown = sorted(set(raw) - set(KNOWN_FEATURES))
    if unknown:
        logger.info("license_unknown_features ignored=%s", unknown)
    features = frozenset(raw) & frozenset(KNOWN_FEATURES)
    if not features:
        raise LicenseError("grants no feature this build has")

    return License(
        customer=customer.strip()[:200],
        features=features,
        issued_at=issued_at,
        expires_at=expires_at,
        not_before=not_before,
    )


def configured_token(env: dict[str, str] | None = None) -> tuple[str | None, str]:
    """``(token, where)`` — where names the source for the log, never the value."""
    env = os.environ if env is None else env
    value = (env.get(ENV_KEY) or "").strip()
    if value:
        return value, ENV_KEY
    path = (env.get(ENV_FILE) or "").strip()
    if path:
        try:
            p = Path(path).expanduser()
            if p.stat().st_size > _MAX_TOKEN_BYTES:
                raise LicenseError("licence file too large")
            return p.read_text(encoding="utf-8").strip() or None, ENV_FILE
        except (OSError, LicenseError) as exc:
            logger.warning(
                "license_file_unreadable %s=%s err=%s — running as the community "
                "edition", ENV_FILE, path, type(exc).__name__,
            )
            return None, ENV_FILE
    return None, ""


def load_license(env: dict[str, str] | None = None) -> License | None:
    """The configured licence, or None for the community edition. Logs why."""
    token, where = configured_token(env)
    if token is None:
        if not where:
            logger.info("license_absent — community edition (set %s or %s to "
                        "enable enterprise features)", ENV_KEY, ENV_FILE)
        return None
    try:
        lic = verify(token)
    except LicenseError as exc:
        # The reason, never the token: it names a customer.
        logger.warning(
            "license_invalid source=%s reason=%s — enterprise features are NOT "
            "mounted; running as the community edition", where, exc,
        )
        return None
    logger.info(
        "license_valid customer=%s features=%s expires_at=%s",
        lic.customer, ",".join(sorted(lic.features)), lic.expires_at.isoformat(),
    )
    return lic


__all__ = [
    "ALGORITHM",
    "ISSUER",
    "KNOWN_FEATURES",
    "PUBLIC_KEY_PEM",
    "License",
    "LicenseError",
    "configured_token",
    "load_license",
    "verify",
]
