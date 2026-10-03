# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Generic OIDC (Keycloak et al.) id_token verification.

The web app (next-auth) runs the OIDC dance and forwards the id_token to
``POST /api/auth/oidc`` (src/ee/sso/router.py). Here the token is checked against the issuer's own
signing keys — not against a tokeninfo endpoint, which a self-hosted IdP may
not have — and the claims are handed back only if all of these hold:

  * the signature verifies with a key from the issuer's JWKS,
  * ``iss`` equals the configured issuer (a trailing slash on either side is
    ignored: Auth0 and Azure AD v1 put one in ``iss``, Keycloak does not),
  * ``aud`` contains the client id, as OIDC Core requires of an id_token; when
    ``aud`` names several audiences, ``azp`` must also be the client id,
  * a Keycloak ``typ`` claim, when present, is ``ID``: a Keycloak access token
    for the same client carries the user's email too and must not pass as an
    id_token,
  * ``exp`` is in the future (PyJWT, with a small leeway).

Configuration is env-only and the feature is off unless both the issuer and
the client id are set:

    OIDC_ISSUER        e.g. https://sso.example.com/realms/celmis
    OIDC_CLIENT_ID     e.g. celmis-web
    OIDC_ADMIN_ROLE    optional — a role/group that grants global is_admin
    OIDC_ADMIN_ROLE_SYNC  optional "true" — also REVOKE is_admin when absent

The web side reads the same values as ``AUTH_OIDC_ISSUER`` /
``AUTH_OIDC_CLIENT_ID``; those names are accepted here as a fallback so one
``.env`` can serve both containers.

Discovery and JWKS are fetched through :func:`src.http.build_client` with
the issuer's host as the one extra allowed destination — it came from the
operator's configuration, not from the token.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

logger = logging.getLogger(__name__)

#: How long a fetched JWKS is trusted before it is fetched again. An unknown
#: ``kid`` forces a refetch regardless (key rotation), rate-limited below.
JWKS_TTL_SECONDS = 3600
#: Minimum gap between forced refetches, so a stream of tokens with a junk
#: ``kid`` cannot turn the endpoint into a JWKS-fetching amplifier.
JWKS_MIN_REFRESH_SECONDS = 30
#: Clock skew tolerated on exp/iat/nbf.
LEEWAY_SECONDS = 30

_ALLOWED_ALGS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                 "ES256", "ES384", "ES512"]


class OidcError(Exception):
    """The token was refused. The message is safe to log, not to echo."""


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    admin_role: str = ""
    admin_role_sync: bool = False


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def oidc_config() -> OidcConfig | None:
    """The configured provider, or None when OIDC sign-in is off."""
    # Stripped once here: it is the identity key stored in users.oidc_iss and
    # the base of the discovery URL. The `iss` comparison ignores the slash.
    issuer = _env("OIDC_ISSUER", "AUTH_OIDC_ISSUER").rstrip("/")
    client_id = _env("OIDC_CLIENT_ID", "AUTH_OIDC_CLIENT_ID")
    if not issuer or not client_id:
        return None
    return OidcConfig(
        issuer=issuer,
        client_id=client_id,
        admin_role=_env("OIDC_ADMIN_ROLE"),
        admin_role_sync=_env("OIDC_ADMIN_ROLE_SYNC").lower() in ("1", "true", "yes"),
    )


# ─── JWKS cache ─────────────────────────────────────────────────────


class _JwksCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issuer: str | None = None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = 0.0

    def clear(self) -> None:
        with self._lock:
            self._issuer = None
            self._keys = {}
            self._fetched_at = 0.0

    def get(self, issuer: str, kid: str | None) -> jwt.PyJWK:
        now = time.monotonic()
        with self._lock:
            stale = (self._issuer != issuer
                     or now - self._fetched_at > JWKS_TTL_SECONDS)
            missing = kid is not None and kid not in self._keys
            if stale or (missing and now - self._fetched_at > JWKS_MIN_REFRESH_SECONDS):
                self._keys = _fetch_jwks(issuer)
                self._issuer = issuer
                self._fetched_at = now
            if kid is None:
                # A token without kid is acceptable only when the issuer
                # publishes exactly one key — otherwise it is a guess.
                if len(self._keys) == 1:
                    return next(iter(self._keys.values()))
                raise OidcError("token has no kid and the issuer has several keys")
            key = self._keys.get(kid)
            if key is None:
                raise OidcError(f"unknown signing key kid={kid!r}")
            return key


_cache = _JwksCache()


def reset_cache() -> None:
    """For tests and for an operator changing OIDC_ISSUER at runtime."""
    _cache.clear()


def _get_json(url: str, issuer: str) -> dict[str, Any]:
    from src.http import build_client

    host = urlsplit(url).hostname or ""
    issuer_host = urlsplit(issuer).hostname or ""
    with build_client(timeout=10.0,
                      extra_allowed_hosts=tuple({host, issuer_host})) as client:
        resp = client.get(url)
    resp.raise_for_status()
    return resp.json()


def _fetch_jwks(issuer: str) -> dict[str, jwt.PyJWK]:
    try:
        discovery = _get_json(f"{issuer}/.well-known/openid-configuration", issuer)
        jwks_uri = discovery.get("jwks_uri")
        if not jwks_uri:
            raise OidcError("discovery document has no jwks_uri")
        jwks = _get_json(jwks_uri, issuer)
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"cannot fetch issuer keys: {exc}") from exc

    keys: dict[str, jwt.PyJWK] = {}
    for raw in jwks.get("keys", []):
        if raw.get("use", "sig") != "sig":
            continue
        try:
            key = jwt.PyJWK.from_json(json.dumps(raw))
        except jwt.PyJWTError:
            # An encryption key or an algorithm PyJWT cannot load — skip it,
            # the signing key is usually next to it.
            continue
        keys[str(raw.get("kid", ""))] = key
    if not keys:
        raise OidcError("issuer JWKS has no usable signing keys")
    return keys


# ─── verification ───────────────────────────────────────────────────


def verify_id_token(token: str, config: OidcConfig) -> dict[str, Any]:
    """Return the verified claims, or raise :class:`OidcError`."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise OidcError(f"malformed token: {exc}") from exc
    alg = header.get("alg")
    if alg not in _ALLOWED_ALGS:
        # Refuses "none" and every HMAC alg: an HS256 token "signed" with the
        # public key is the classic confusion attack.
        raise OidcError(f"algorithm {alg!r} not accepted")

    key = _cache.get(config.issuer, header.get("kid"))
    try:
        claims = jwt.decode(
            token,
            key=key.key,
            algorithms=[alg],
            leeway=LEEWAY_SECONDS,
            # iss and aud are checked by hand below: PyJWT compares iss
            # byte-for-byte, and the configured issuer has its trailing slash
            # stripped.
            options={"verify_aud": False, "verify_iss": False,
                     "require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise OidcError("token expired") from exc
    except jwt.PyJWTError as exc:
        raise OidcError(f"invalid token: {exc}") from exc

    iss = claims.get("iss")
    if not isinstance(iss, str) or iss.removesuffix("/") != config.issuer:
        raise OidcError("issuer mismatch")

    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else list(aud or [])
    if config.client_id not in audiences:
        raise OidcError("audience mismatch")
    if len(audiences) > 1 and claims.get("azp") != config.client_id:
        raise OidcError("azp mismatch for a multi-audience token")
    typ = claims.get("typ")
    if typ is not None and typ != "ID":
        raise OidcError(f"token typ {typ!r} is not an id_token")
    return claims


def email_is_verified(claims: dict[str, Any]) -> bool:
    """`email_verified` is a boolean in the spec; some IdPs send "true"."""
    value = claims.get("email_verified")
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return value is True


def token_roles(claims: dict[str, Any], client_id: str) -> set[str]:
    """Every role/group name the token carries for THIS client.

    Keycloak: ``realm_access.roles`` and ``resource_access.<client_id>.roles``
    — only Celmis's own client entry. A client-roles mapper without a client
    filter puts every client of the realm into ``resource_access``; merging
    them would let ``admin`` on, say, the Grafana client grant global Celmis
    admin. Others: a flat ``roles`` or ``groups`` claim. Group paths
    ("/admins") are matched both with and without the leading slash.
    """
    found: set[str] = set()

    def add(values: Any) -> None:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            return
        for v in values:
            if isinstance(v, str) and v:
                found.add(v)
                found.add(v.lstrip("/"))

    realm = claims.get("realm_access")
    if isinstance(realm, dict):
        add(realm.get("roles"))
    resources = claims.get("resource_access")
    if isinstance(resources, dict):
        entry = resources.get(client_id)
        if isinstance(entry, dict):
            add(entry.get("roles"))
    add(claims.get("roles"))
    add(claims.get("groups"))
    return found
