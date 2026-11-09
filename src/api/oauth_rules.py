"""The rules of the OAuth server that are not about a request: what a redirect
URI may be, which scopes a person may hand out, what a ``resource`` may name.

Kept apart from the router so each rule is a function a test can call with a
string, and so the router reads as the sequence of steps it is.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

#: Where a public client may be sent back to over TLS when the operator said
#: nothing: the hosted Claude connector's callback hosts. The callback itself is
#: ``https://claude.ai/api/mcp/auth_callback`` (Anthropic's custom-connector
#: documentation); ``claude.com`` is the address it is being moved to.
DEFAULT_REDIRECT_HOSTS = ("claude.ai", "claude.com")

#: RFC 8252 section 7.3: a native app listens on the loopback interface.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_MAX_URI = 2000
_BAD_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")
#: RFC 7636 section 4.2: 43 to 128 characters of the unreserved set.
_CHALLENGE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_NAME_JUNK = re.compile(r"[\x00-\x1f\x7f<>`]")


def allowed_redirect_hosts() -> tuple[str, ...]:
    """``OAUTH_ALLOWED_REDIRECT_HOSTS``: comma-separated https hosts a public
    client may register. A leading ``*.`` allows every subdomain. Unset means
    :data:`DEFAULT_REDIRECT_HOSTS`; set and empty means none (loopback only)."""
    raw = os.environ.get("OAUTH_ALLOWED_REDIRECT_HOSTS")
    if raw is None:
        return DEFAULT_REDIRECT_HOSTS
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def _parts(uri: str):  # noqa: ANN202
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 — raises ValueError on a malformed port
    except ValueError:
        return None
    return parts


def is_loopback(uri: str) -> bool:
    parts = _parts(uri)
    return bool(parts and parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS)


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    for rule in allowed:
        if rule.startswith("*."):
            if host.endswith(rule[1:]) and host != rule[2:]:
                return True
        elif host == rule:
            return True
    return False


def redirect_problem(uri: str, allowed: tuple[str, ...] | None = None) -> str | None:
    """Why a PUBLIC client may not register ``uri``, or None.

    Loopback over http on any port, or https on an allowlisted host. No other
    scheme (a custom scheme is claimed by whichever app registers it first), no
    fragment, no credentials in the URL.
    """
    if not isinstance(uri, str) or not uri or len(uri) > _MAX_URI:
        return "a redirect URI must be a non-empty string of at most 2000 characters"
    if _BAD_CHARS.search(uri):
        return "a redirect URI may not contain whitespace, control characters or backslashes"
    parts = _parts(uri)
    if parts is None:
        return "not a valid URI"
    if "#" in uri or parts.fragment:
        return "a redirect URI may not carry a fragment"
    if "@" in parts.netloc:
        return "a redirect URI may not carry credentials"
    host = (parts.hostname or "").lower()
    if not host:
        return "a redirect URI needs a host"
    if parts.scheme == "http":
        if host in LOOPBACK_HOSTS:
            return None
        return "http redirect URIs are accepted for localhost, 127.0.0.1 and [::1] only"
    if parts.scheme == "https":
        if _host_allowed(host, allowed_redirect_hosts() if allowed is None else allowed):
            return None
        return (f"https redirect host {host!r} is not allowed on this server "
                "(see OAUTH_ALLOWED_REDIRECT_HOSTS)")
    return "redirect URIs must be http://localhost-style loopback or https"


def _given_problem(given: object) -> bool:
    """Whether a redirect URI SENT with a request is unsafe to follow, whatever was registered.

    Python reads ``http://evil.example\\@localhost/cb`` as host ``localhost``; a browser
    treats the backslash as a slash and goes to ``evil.example``. So a URI with a
    backslash, a control character or whitespace, a percent-encoded backslash,
    credentials or a fragment is never followed."""
    if not isinstance(given, str) or not given or len(given) > _MAX_URI:
        return True
    if _BAD_CHARS.search(given) or "#" in given or "%5c" in given.lower():
        return True
    parts = _parts(given)
    return parts is None or "@" in parts.netloc or not parts.hostname


def canonical_redirect(registered: list[str], given: str) -> str | None:
    """The address to send the browser to for ``given``, or None when it is not allowed.

    Exact match with a registered URI, with the one exception RFC 8252 section 7.3
    requires: for a loopback URI the port is not part of the registration, because a
    native app takes whatever port is free when it starts. Scheme, host, path and
    query still have to match, so ``localhost`` does not stand in for ``127.0.0.1``.

    The answer is built from the REGISTERED URI plus the validated integer port,
    never from the caller's text, so nothing in ``given`` beyond that port reaches
    the Location the browser follows."""
    if _given_problem(given):
        return None
    if given in registered:
        return given
    want = _parts(given)
    if want is None or not is_loopback(given):
        return None
    for item in registered:
        if not is_loopback(item):
            continue
        have = _parts(item)
        if (have and have.hostname == want.hostname and have.path == want.path
                and have.query == want.query):
            host = f"[{have.hostname}]" if ":" in (have.hostname or "") else have.hostname
            netloc = f"{host}:{want.port}" if want.port else host
            return f"http://{netloc}{have.path}" + (f"?{have.query}" if have.query else "")
    return None


def redirect_matches(registered: list[str], given: str) -> bool:
    """Whether ``given`` may be used as the redirect of a client registered with ``registered``."""
    return canonical_redirect(registered, given) is not None


def host_of(uri: str) -> str:
    """The host a redirect goes to, for the person who is asked to approve it."""
    parts = _parts(uri)
    if parts is None or not parts.hostname:
        return ""
    host = parts.hostname
    return f"[{host}]" if ":" in host else host


def valid_challenge(value: str) -> bool:
    return bool(_CHALLENGE.match(value or ""))


def clean_client_name(raw: object) -> str:
    """A name safe to show a person: printable, one line, bounded."""
    text = _NAME_JUNK.sub(" ", str(raw or ""))
    text = re.sub(r"\s+", " ", text).strip()[:80]
    return text or "MCP client"


# ─── resource (RFC 8707) ─────────────────────────────────────────────

#: The MCP endpoints a token can be bound to, relative to the public base.
RESOURCE_PATHS = ("/mcp", "/mcp/dev")


def resource_problem(resource: str | None, base: str) -> str | None:
    """Why ``resource`` is not one of this server's MCP endpoints, or None.

    ``base`` is the public base of the API as the client reached it
    (``https://host/backend``). A resource is a URI with no fragment that names
    one of :data:`RESOURCE_PATHS` under that base, with or without a trailing
    slash. Anything else is ``invalid_target``: a token minted for an address
    this server does not serve would be a token for somebody else's resource.
    """
    if resource is None or resource == "":
        return None
    parts = _parts(resource)
    if parts is None or parts.fragment or "#" in resource or not parts.scheme or not parts.netloc:
        return "resource must be an absolute URI without a fragment"
    mine = _parts(base)
    if mine is None:
        return "this server has no public address to bind a token to"
    if (parts.scheme.lower(), (parts.hostname or "").lower(), parts.port) != (
            mine.scheme.lower(), (mine.hostname or "").lower(), mine.port):
        return "resource is not an address of this server"
    path = parts.path.rstrip("/")
    prefix = mine.path.rstrip("/")
    if parts.query or path not in {prefix + p for p in RESOURCE_PATHS}:
        return "resource is not an MCP endpoint of this server"
    return None


def normalise_resource(resource: str | None) -> str | None:
    """The stored spelling: no trailing slash (the form the MCP specification prefers)."""
    if not resource:
        return None
    return resource.rstrip("/")


# ─── scopes ──────────────────────────────────────────────────────────


def read_scopes() -> list[str]:
    from src.mcp_server.token_store import DEV_SCOPES, READ_SCOPES

    return [*READ_SCOPES, *DEV_SCOPES]


def write_scopes() -> list[str]:
    from src.mcp_server.token_store import WRITE_SCOPES

    return list(WRITE_SCOPES)


def known_scopes() -> list[str]:
    return [*read_scopes(), *write_scopes()]


def split_scope(scope: str | None) -> list[str]:
    return list(dict.fromkeys(s for s in (scope or "").split() if s))


def scope_plan(requested: list[str], role: str | None, *, is_admin: bool) -> tuple[list[str], list[tuple[str, str]]]:
    """What a person may hand out, given what the client asked for.

    Returns ``(granted, withheld)``. Nothing requested is the read-only default.
    A read scope asked for is granted; a write scope only when the person's role
    in the workspace may use it (the same table as ``mcp_access``), and one that
    is not is withheld with the reason, never silently turned into a token that
    fails later. A scope this server does not know is refused by the caller
    before it gets here.
    """
    from src.api.routers.mcp_access import _write_scope_roles

    reads = read_scopes()
    if not requested:
        return list(reads), []
    roles = _write_scope_roles()
    granted: list[str] = []
    withheld: list[tuple[str, str]] = []
    for scope in requested:
        if scope in reads:
            granted.append(scope)
        elif scope in roles:
            if is_admin or role in roles[scope]:
                granted.append(scope)
            else:
                needs = ", ".join(sorted(roles[scope]))
                withheld.append((scope, f"needs one of these roles on the workspace: {needs}"))
    return (granted or list(reads)), withheld


__all__ = [
    "DEFAULT_REDIRECT_HOSTS", "LOOPBACK_HOSTS", "RESOURCE_PATHS", "allowed_redirect_hosts",
    "canonical_redirect", "clean_client_name", "host_of", "is_loopback", "known_scopes", "normalise_resource",
    "read_scopes", "redirect_matches", "redirect_problem", "resource_problem", "scope_plan",
    "split_scope", "valid_challenge", "write_scopes",
]
