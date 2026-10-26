"""The address this API is reached at from outside, as the MCP client sees it.

Behind the bundled proxy the API never sees its own public URL. The proxy
terminates TLS, takes the `/backend` prefix off the path, and hands over a plain
`http://api:8000` request, so everything the API derives from the request alone
is wrong in the one way that matters for sign-in discovery: the scheme is
`http`, and the prefix is missing. An MCP client that reads a metadata document
carrying `http://host/mcp` while it connected to `https://host/backend/mcp/dev/`
either refuses it (the resource is not the one it asked) or follows it to a
place that does not exist.

This module answers one question, "what is the public base of this API?", for
every place that has to put a URL into a document or a header: the protected
resource and authorization server metadata, and the `WWW-Authenticate` challenge
on a 401.

Where the answer comes from, strongest first:

1. `MCP_OAUTH_ISSUER`, when the operator set it: the full public base including
   any prefix (`https://celmis.example.com/backend`). Nothing is derived.
2. The host and scheme the CLIENT used, from `X-Forwarded-Host` /
   `X-Forwarded-Proto` (what the proxy sets) or the `Host` header, but only when
   that host is one the MCP endpoint itself would serve: localhost, the host of
   `PUBLIC_BASE_URL`, or one named in `MCP_ALLOWED_HOSTS` (the same list the
   DNS-rebinding guard enforces). With `PUBLIC_BASE_URL` set, a host nobody
   declared is not reflected back into a document. For the `PUBLIC_BASE_URL`
   host itself, an `https://` there outranks a forwarded `http`: a proxy that is
   not told to trust the one in front of it reports its own plain connection,
   not the client's. That is the only place the scheme is corrected. A host
   declared ONLY in `MCP_ALLOWED_HOSTS` keeps whatever scheme the proxy reported,
   so behind a TLS terminator (which the bundled `:80` proxy cannot see through)
   the operator has to say `https` once: `PUBLIC_BASE_URL=https://host`, or
   `MCP_OAUTH_ISSUER=https://host/backend`. A warning in the log says so the
   first time such a host is named over plain `http`.
3. Otherwise `PUBLIC_BASE_URL`. With no `PUBLIC_BASE_URL` and no declared host
   (an install nobody has configured yet, where the MCP endpoint itself answers
   421) the server hands the caller back the host it sent, `X-Forwarded-Host`
   or `Host`, with the scheme it reported. Only that caller sees the answer, and
   it carries `Cache-Control: no-store`.

The forwarded headers are read from whoever sends them: there is no
`CELMIS_TRUST_PROXY` gate here, which is why a host has to be declared before it
is written into a document for anyone but the caller itself.

The path prefix comes from `X-Forwarded-Prefix` when the proxy sends it (the
bundled Caddyfiles do), else from the product's own convention: a request that
arrived through a proxy on the `PUBLIC_BASE_URL` host is on the app host, where
the API sits under `/backend` (the same constant the webhook URLs use). A
separate API domain has no prefix.

Nothing here is a trust decision about WHO the caller is. It only decides which
spelling of the server's own address goes into a document, and the JWT
audience/issuer the verifier checks are untouched.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Where the API sits behind the bundled proxy, which strips it before forwarding.
#: The same value as `webhook_install.PROXY_PREFIX` and `webhooks._PROXY_PREFIX`.
PROXY_PREFIX = "/backend"

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(
    rf"^(?:\[[0-9A-Fa-f:.]{{2,45}}\]|{_LABEL}(?:\.{_LABEL})*)(?::[0-9]{{1,5}})?$"
)
_PREFIX = re.compile(r"^(?:/[A-Za-z0-9._~-]{1,64}){1,4}$")

#: Hosts already named over plain `http` in a log line (see `_note_plain_http`).
_PLAIN_HTTP_NOTED: set[str] = set()


def _first(value: str | None) -> str:
    """The first entry of a comma-separated forwarded header, trimmed."""
    return (value or "").split(",")[0].strip()


def _valid_host(host: str) -> bool:
    return bool(host) and len(host) <= 261 and _HOST.match(host) is not None


def _clean_prefix(raw: str) -> str | None:
    """`/backend` for a sane prefix, `""` for none, None for garbage."""
    raw = raw.strip()
    if raw in ("", "/"):
        return ""
    raw = raw.rstrip("/")
    if not _PREFIX.match(raw) or any(seg in (".", "..") for seg in raw.split("/")):
        return None
    return raw


def configured_public_base() -> str:
    """`PUBLIC_BASE_URL` as the operator wrote it, or "".

    The settings field first (it reads `.env` too, like every other consumer of
    this value), then both spellings of the environment variable the MCP
    transport guard reads.
    """
    try:
        from src.config import get_settings

        raw = str(getattr(get_settings(), "public_base_url", "") or "").strip()
        if raw:
            return raw
    except Exception:  # noqa: BLE001 — settings must never break discovery
        pass
    for name in ("CELMIS_PUBLIC_BASE_URL", "PUBLIC_BASE_URL"):
        raw = (os.environ.get(name) or "").strip()
        if raw:
            return raw
    return ""


def _configured_parts() -> tuple[str, str, str] | None:
    """(origin, netloc, path) of the configured public base, or None."""
    raw = configured_public_base()
    if not raw:
        return None
    parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    if parts.scheme not in ("http", "https") or not parts.netloc or not _valid_host(parts.netloc):
        return None
    return f"{parts.scheme}://{parts.netloc}", parts.netloc.lower(), parts.path.rstrip("/")


def _explicit_base() -> str | None:
    """`MCP_OAUTH_ISSUER`: the operator's full answer, or None.

    Checked like every other part of the address: the value is written into a
    quoted `resource_metadata="..."` header parameter and into JSON documents,
    so a path with a quote, a space or a `..` in it would break the header.
    """
    raw = (os.environ.get("MCP_OAUTH_ISSUER") or "").strip().rstrip("/")
    if not raw:
        return None
    parts = urlsplit(raw)
    prefix = _clean_prefix(parts.path)
    if (parts.scheme not in ("http", "https") or not _valid_host(parts.netloc)
            or parts.query or parts.fragment or prefix is None):
        logger.warning(
            "mcp_oauth_issuer_ignored — needs http(s)://host[:port][/prefix], where the "
            "prefix is plain path segments (letters, digits, . _ ~ -)"
        )
        return None
    return f"{parts.scheme}://{parts.netloc}{prefix}"


def _note_plain_http(host: str) -> None:
    """Say once, in the log, that a named host is being written down as `http`.

    The bundled `:80` proxy overwrites `X-Forwarded-Proto` with its own plain
    connection, so behind a TLS terminator a host that is declared only in
    `MCP_ALLOWED_HOSTS` is named `http://` in every document, and a client that
    registers against that address posts to a place that redirects or refuses.
    A plain-http install looks exactly the same from here, so this only tells;
    it never changes the answer. Local and bare-IP addresses are not mentioned:
    nobody terminates TLS for those.
    """
    bare = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0].lstrip("[")
    if host in _PLAIN_HTTP_NOTED or len(_PLAIN_HTTP_NOTED) >= 64:
        return
    if bare == "localhost" or bare.endswith(".localhost"):
        return
    try:
        ipaddress.ip_address(bare)
        return
    except ValueError:
        pass
    _PLAIN_HTTP_NOTED.add(host)
    logger.warning(
        "mcp_public_base_plain_http host=%s — sign-in documents and the 401 challenge name "
        "http://%s. If people reach it over https (a TLS terminator in front of the bundled "
        "proxy), set PUBLIC_BASE_URL=https://%s or MCP_OAUTH_ISSUER=https://%s/backend.",
        host, host, host, host,
    )


def host_is_declared(host: str) -> bool:
    """Whether the MCP endpoint is willing to be reached at `host`.

    Asked of the transport guard's own list, so a host that would get `421` is
    never the host a document names, and the two cannot drift apart.
    """
    host = host.lower()
    try:
        from src.mcp_server.http_app import _transport_security

        sec = _transport_security()
    except Exception:  # noqa: BLE001
        sec = None
    if sec is None:
        return False
    for allowed in sec.allowed_hosts:
        allowed = str(allowed).lower()
        if allowed == host:
            return True
        if allowed.endswith(":*") and host.split(":")[0] == allowed[:-2]:
            return True
    return False


def public_base(headers: Mapping[str, str], scheme: str, netloc: str) -> str:
    """The public base URL of this API, without a trailing slash.

    `headers` has lower-case names. `scheme` and `netloc` are the request's own,
    the last resort.
    """
    explicit = _explicit_base()
    if explicit:
        return explicit

    configured = _configured_parts()

    host = _first(headers.get("x-forwarded-host")) or _first(headers.get("host")) or netloc
    host = host.strip().lower()
    proto = _first(headers.get("x-forwarded-proto")).lower() or scheme
    if proto not in ("http", "https"):
        proto = scheme if scheme in ("http", "https") else "https"

    if _valid_host(host) and host_is_declared(host):
        if proto == "http" and configured and host == configured[1] and configured[0].startswith("https://"):
            # The bundled `:80` proxy sits behind a TLS terminator and, trusting
            # nobody, overwrites X-Forwarded-Proto with its own plain `http`. The
            # operator wrote `https://` for this very host: that is how people
            # reach it, and an `http` address here would not work for them.
            proto = "https"
        if proto == "http":
            _note_plain_http(host)
        origin = f"{proto}://{host}"
    elif configured:
        origin, host = configured[0], configured[1]
    elif _valid_host(host):
        origin = f"{proto}://{host}"
    else:
        origin = f"{scheme}://{netloc}"

    sent = headers.get("x-forwarded-prefix")
    prefix = _clean_prefix(_first(sent)) if sent is not None else None
    if prefix is None:
        prefix = ""
        proxied = any(h in headers for h in ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"))
        if configured and proxied and host == configured[1]:
            prefix = configured[2] or PROXY_PREFIX
    return f"{origin}{prefix}"


def base_from_scope(scope: Mapping) -> str:
    """`public_base` for a raw ASGI scope."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    server = scope.get("server") or ("localhost", 80)
    return public_base(headers, str(scope.get("scheme") or "http"), f"{server[0]}:{server[1]}")


def base_from_request(request) -> str:  # noqa: ANN001 — a Starlette Request
    """`public_base` for a Starlette/FastAPI request."""
    headers = {k.lower(): v for k, v in request.headers.items()}
    return public_base(headers, request.url.scheme, request.url.netloc)


def protected_resource_metadata_url(base: str, resource_path: str) -> str:
    """Where the metadata document of the MCP endpoint at `resource_path` lives.

    Under the API's own public base, not at the host root: the proxy always
    routes that prefix to the API, whereas the host root belongs to the web app.
    `resource_path` is the endpoint as the API mounts it (`/mcp`, `/mcp/dev`).
    """
    return f"{base}/.well-known/oauth-protected-resource{resource_path.rstrip('/')}"


__all__ = [
    "PROXY_PREFIX", "base_from_request", "base_from_scope", "configured_public_base",
    "host_is_declared", "protected_resource_metadata_url", "public_base",
]
