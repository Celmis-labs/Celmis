# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The enterprise licence: an offline, signed token, checked at start-up.

WHAT A LICENCE IS
-----------------
A JWT signed with Ed25519 (``alg: EdDSA``) by a private key that never enters
this repository. Its claims:

    iss       "celmis-licensing" — nothing else is a Celmis licence
    sub       the customer, as it is shown to an administrator
    features  the enterprise features it grants, e.g. ["sso", "analytics", "sharing"]
    iat, exp  required; nbf optional
    seats     optional, a positive integer — how many people the installation
              may hold. Absent: unlimited (a licence issued before seats
              existed keeps working unchanged). Without a licence the limit is
              FREE_SEATS (src/users/seats.py). A licence never gives LESS than
              that: the limit in force is the larger of the claim and
              FREE_SEATS.
    sso_seats optional, a positive integer — how many people may sign in
              through single sign-on. Absent on a licence that grants "sso":
              unlimited. A licence that does not grant "sso" leaves the free
              SSO allowance in force (FREE_SSO_SEATS), and a licence never
              gives less than that either.

Both limits only ever refuse ADDING a person (src/users/seats.py); nobody who
already has an account is disabled by a limit or by the end of a licence.

A claim that is PRESENT but not a positive integer up to MAX_SEATS (a float, a
string, a boolean, zero, null, too large) fails CLOSED: it is read as the
community limit, and logged at ERROR by claim name — never by token, never by
value. A malformed limit must not be the way to unlimited seats; refusing the
whole licence for it would instead take SSO and analytics away from somebody
who paid for them, so the rest of the licence stays in force. A claim that is
absent is the only unlimited.

It is verified here, against :data:`PUBLIC_KEY_PEM`, and nowhere else. There
is no call home and no revocation list: self-hosting has to work in a closed
network, so the only facts a licence can carry are the ones signed into it.

WHERE IT COMES FROM
-------------------
``CELMIS_LICENSE_KEY`` (the token itself) or ``CELMIS_LICENSE_FILE`` (a path
to a file holding it), or — when neither is set — the token a global admin
entered on ``/admin/health`` (``src/ee/license_store.py``). The variable wins
over the file — it is the one a container orchestrator injects, and a stale
file left in a volume must not outrank it — and either environment source
wins over the UI, which refuses to save while one is set.

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from src.ee.sso.allowance import FREE_SSO_SEATS
from src.users.seats import FREE_SEATS

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
KNOWN_FEATURES: tuple[str, ...] = ("sso", "analytics", "sharing")

#: The largest `seats` / `sso_seats` a licence may carry. A bigger number is
#: not a limit anybody meant, so the claim is ignored like any malformed one.
MAX_SEATS = 1_000_000

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
    #: How many people the installation may hold under this licence — the
    #: limit in force, never below FREE_SEATS; None = unlimited.
    seats: int | None = None
    #: How many may sign in through single sign-on — never below
    #: FREE_SSO_SEATS; None = unlimited (only meaningful when the licence
    #: grants "sso").
    sso_seats: int | None = None

    def grants(self, feature: str) -> bool:
        return feature in self.features

    def expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at

    def summary(self) -> dict[str, Any]:
        """What the rest of the process may know: never the token."""
        return {
            "edition": "enterprise",
            "customer": self.customer,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "features": sorted(self.features),
            "seats": self.seats,
            "sso_seats": self.sso_seats,
        }

    def sso_allowance(self, free: int) -> int | None:
        """The SSO limit this licence puts in force: its `sso_seats` claim
        (None = unlimited) when it grants single sign-on, else the free
        allowance `free` — a licence for other features lifts nothing here."""
        return self.sso_seats if self.grants("sso") else free


def _public_key() -> Ed25519PublicKey:
    key = load_pem_public_key(PUBLIC_KEY_PEM.encode("ascii"))
    if not isinstance(key, Ed25519PublicKey):  # pragma: no cover — a bad edit
        raise LicenseError("the embedded licensing key is not an Ed25519 key")
    return key


def _ts(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def _seat_claim(claims: dict[str, Any], name: str, free: int) -> int | None:
    """`seats` / `sso_seats`: the limit this claim puts in force.

    ABSENT is unlimited (None). A positive integer up to MAX_SEATS is that
    number, but never below ``free`` — a licence does not take away what the
    community edition has. PRESENT and anything else (a float, a string, a
    boolean, zero, a negative, null, a huge number) fails closed to ``free``,
    logged at ERROR BY NAME: never the token, never the value."""
    if name not in claims:
        return None
    raw = claims[name]
    if type(raw) is not int or not 1 <= raw <= MAX_SEATS:
        logger.error("license_claim_malformed claim=%s — not a positive integer up "
                     "to %d; read as the community limit of %d", name, MAX_SEATS, free)
        return free
    return max(raw, free)


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
        seats=_seat_claim(claims, "seats", FREE_SEATS),
        sso_seats=_seat_claim(claims, "sso_seats", FREE_SSO_SEATS),
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


#: Where the licence in force came from. Labels, never values.
SOURCE_ENV_KEY = "env_key"
SOURCE_ENV_FILE = "env_file"
SOURCE_UI = "ui"
_SOURCE_OF = {ENV_KEY: SOURCE_ENV_KEY, ENV_FILE: SOURCE_ENV_FILE}

#: `Loaded.problem` when the UI slot exists but could not be read.
STORED_UNREADABLE = "stored licence unreadable"


def env_source(env: dict[str, str] | None = None) -> str | None:
    """Which environment variable manages the licence, if any — set and
    non-empty, whether or not what it holds verifies. An operator who set one
    owns the licence; the UI must not store something it would shadow."""
    env = os.environ if env is None else env
    if (env.get(ENV_KEY) or "").strip():
        return SOURCE_ENV_KEY
    if (env.get(ENV_FILE) or "").strip():
        return SOURCE_ENV_FILE
    return None


@dataclass(frozen=True)
class Loaded:
    """The outcome of looking for a licence: what is in force, where it came
    from, and — when one was given and refused — why. Never the token."""

    license: License | None
    source: str | None
    problem: str | None = None


def load(
    env: dict[str, str] | None = None,
    *,
    stored: Callable[[], str | None] | None = None,
) -> Loaded:
    """The licence in force. Precedence: ``CELMIS_LICENSE_KEY`` >
    ``CELMIS_LICENSE_FILE`` > the token stored from the UI (`stored`).

    The environment wins even when what it holds does not verify: falling
    back to the stored token then would make the licence in force depend on
    whether the operator's variable happens to be valid today.
    """
    token, where = configured_token(env)
    source = _SOURCE_OF.get(where)
    if source is None and stored is not None:
        try:
            token = stored()
        except Exception as exc:  # noqa: BLE001 — a licence problem never stops the process
            # A licence may be stored but could not be read. Say so, rather
            # than look exactly like an installation that never had one.
            logger.warning("license_stored_unreadable err=%s — running as the "
                           "community edition", type(exc).__name__)
            return Loaded(None, SOURCE_UI, STORED_UNREADABLE)
        source = SOURCE_UI if token else None
    if token is None:
        if source is None:
            logger.info("license_absent — community edition (set %s or %s, or "
                        "enter a licence on /admin/health, to enable enterprise "
                        "features)", ENV_KEY, ENV_FILE)
            return Loaded(None, None)
        return Loaded(None, source, "licence file unreadable")
    try:
        lic = verify(token)
    except LicenseError as exc:
        # The reason, never the token: it names a customer.
        logger.warning(
            "license_invalid source=%s reason=%s — enterprise features are NOT "
            "mounted; running as the community edition", source, exc,
        )
        return Loaded(None, source, str(exc))
    logger.info(
        "license_valid source=%s customer=%s features=%s expires_at=%s seats=%s "
        "sso_seats=%s",
        source, lic.customer, ",".join(sorted(lic.features)),
        lic.expires_at.isoformat(),
        lic.seats if lic.seats is not None else "unlimited",
        lic.sso_seats if lic.sso_seats is not None else "unlimited",
    )
    return Loaded(lic, source)


def load_license(
    env: dict[str, str] | None = None,
    *,
    stored: Callable[[], str | None] | None = None,
) -> License | None:
    """The configured licence, or None for the community edition. Logs why."""
    return load(env, stored=stored).license


__all__ = [
    "ALGORITHM",
    "ISSUER",
    "KNOWN_FEATURES",
    "MAX_SEATS",
    "PUBLIC_KEY_PEM",
    "SOURCE_ENV_FILE",
    "SOURCE_ENV_KEY",
    "SOURCE_UI",
    "STORED_UNREADABLE",
    "License",
    "LicenseError",
    "Loaded",
    "configured_token",
    "env_source",
    "load",
    "load_license",
    "verify",
]
