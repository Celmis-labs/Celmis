"""What the API says to the browser about itself: security headers, and where
its redirects point.

Two small ASGI middlewares, both written as plain ASGI (not
``BaseHTTPMiddleware``) so streaming responses and the MCP transport pass
through untouched.

SecurityHeadersMiddleware
    ``X-Content-Type-Options``, ``Referrer-Policy``, ``Permissions-Policy``,
    ``X-Frame-Options: DENY``, a locked-down ``Content-Security-Policy`` (an API
    returns JSON; it has no business loading anything), and
    ``Strict-Transport-Security`` when the request arrived over https. A header
    the route already set is left alone. ``CELMIS_CSP=off`` drops the CSP only
    (the switch described in ``.env.example``); ``report-only`` sends it as
    ``Content-Security-Policy-Report-Only``.

PublicRedirectMiddleware
    Starlette answers ``/mcp`` with ``307 Location: http://host/mcp/`` built
    from the connection it sees: plain http, and without the ``/backend``
    prefix the proxy took off. A client that follows it repeats its
    ``Authorization`` header over http. Same-host redirects are rewritten onto
    the public address (``src.mcp_server.public_url.public_base``, the one place
    that already knows scheme, host and prefix). Redirects to other hosts - an
    OAuth ``redirect_uri``, an IdP - are not touched.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Keep every response out of search results. The web app sends the same value
#: (web/lib/csp.ts). robots.txt does NOT disallow crawling on purpose: a crawler
#: that may not fetch a page never sees this header, and a page already in the
#: index would stay there.
ROBOTS_TAG = "noindex, nofollow, noarchive"

_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)

#: Pages of the API that are HTML and load their own assets.
_HTML_DOC_PATHS = ("/docs", "/redoc")

#: An API response is data. Nothing may load, frame or submit anywhere.
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
#: The OAuth consent page is the one HTML document the API serves itself; its
#: <style> is inline and its form posts back to the API.
_CONSENT_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)


def csp_mode() -> str:
    """``enforce`` (default) | ``report-only`` | ``off``. Anything else enforces."""
    raw = os.environ.get("CELMIS_CSP", "").strip().lower()
    return raw if raw in ("enforce", "report-only", "off") else "enforce"


def _is_https(scope: Scope, headers: dict[str, str]) -> bool:
    if scope.get("scheme") == "https":
        return True
    if headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https":
        return True
    try:
        from src.mcp_server.public_url import configured_public_base

        return configured_public_base().lower().startswith("https://")
    except Exception:  # noqa: BLE001 — a header must not take a request down
        return False


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        req_headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers") or []}
        path = scope.get("path", "")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                extra: list[tuple[bytes, bytes]] = []

                def put(name: str, value: str) -> None:
                    if name.encode().lower() not in present:
                        extra.append((name.encode(), value.encode("latin-1")))

                put("X-Content-Type-Options", "nosniff")
                put("Referrer-Policy", "strict-origin-when-cross-origin")
                put("Permissions-Policy", _PERMISSIONS_POLICY)
                put("X-Frame-Options", "DENY")
                # A self-hosted install is private: nothing it serves belongs in
                # a search index, the API documents included.
                put("X-Robots-Tag", ROBOTS_TAG)
                if _is_https(scope, req_headers):
                    put("Strict-Transport-Security", "max-age=31536000")
                mode = csp_mode()
                if mode != "off" and not path.startswith(_HTML_DOC_PATHS):
                    csp = _CONSENT_CSP if path.startswith("/oauth/authorize") else _API_CSP
                    put("Content-Security-Policy" if mode == "enforce"
                        else "Content-Security-Policy-Report-Only", csp)
                if extra:
                    message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_wrapper)


class PublicRedirectMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and 300 <= message["status"] < 400:
                message = _rewrite_location(scope, message)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _rewrite_location(scope: Scope, message: Message) -> Message:
    headers = list(message.get("headers", []))
    for i, (name, value) in enumerate(headers):
        if name.lower() != b"location":
            continue
        try:
            target = _public_location(scope, value.decode("latin-1"))
        except Exception:  # noqa: BLE001 — leave the redirect as it was
            return message
        if target is not None:
            headers[i] = (name, target.encode("latin-1"))
            return {**message, "headers": headers}
        break
    return message


def _public_location(scope: Scope, location: str) -> str | None:
    """``location`` on the public address, or None when it is not ours."""
    from src.mcp_server.public_url import base_from_scope

    parts = urlsplit(location)
    if parts.scheme or parts.netloc:
        req = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
        own_host = (req.get("x-forwarded-host", "").split(",")[0].strip()
                    or req.get("host", "")).lower()
        if parts.netloc.lower() != own_host:
            return None  # a redirect to somebody else is not ours to move
    elif not location.startswith("/"):
        return None  # relative to the current path: already resolves correctly
    path = parts.path or "/"
    base = base_from_scope(scope)
    prefix = urlsplit(base).path.rstrip("/")
    if prefix and (path == prefix or path.startswith(prefix + "/")):
        base = base[: len(base) - len(prefix)]  # the app already named the prefix
    out = f"{base}{path}"
    if parts.query:
        out += f"?{parts.query}"
    if parts.fragment:
        out += f"#{parts.fragment}"
    return out
