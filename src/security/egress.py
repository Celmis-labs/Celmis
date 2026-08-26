"""Network egress whitelist — an httpx transport that blocks traffic to hosts
outside the allowed list.

The allowlist names hosts on the PUBLIC INTERNET. There is a second, separate
question — may this client open a socket to something that never leaves the
network at all (a model server on loopback, an `ollama` container on the
compose bridge)? That is `allow_private_network`, off by default, because a
transport that silently trusts private addresses is an SSRF hole: the cloud
metadata endpoint 169.254.169.254 is "private" by `ipaddress`'s reckoning and
is the single address an attacker most wants you to fetch. Link-local is
excluded by name here for exactly that reason.

The distinction is what lets an air-gapped install run with `egress_allowed_hosts`
EMPTY: nothing may be reached on the internet, and the embeddings server is
reachable because it is not on the internet.

There are two transports — httpx dispatches a sync client through
`handle_request` and an async one through `handle_async_request`, so one class
cannot serve both — and exactly ONE copy of the decision. Both inherit it from
:class:`_WhitelistPolicy`, and :func:`host_is_allowed` is the only place the
three rules are written down. Two checkers that decide separately eventually
disagree, and the one that disagrees is the one nobody exercised that day.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class EgressBlockedError(RuntimeError):
    """Raised when an httpx client tries to contact a host that is not allowed."""


def is_private_destination(host: str) -> bool:
    """True when every address `host` resolves to is off-internet.

    Resolution, not naming: a hostname is trusted because of where it actually
    points, so `ollama` on a compose bridge passes and `evil.example.com`
    fails whatever it is called. ALL resolved addresses must qualify — one
    public A record among them and the answer is False, since httpx is free to
    pick that one.

    Excluded on purpose:
      * link-local (169.254/16, fe80::/10) — cloud instance metadata,
      * anything that fails to resolve — unknown is not private.

    Caveat worth stating out loud: the DNS lookup itself is a packet. For a
    literal IP or a name in /etc/hosts there is none; for a name served by a
    resolver outside the box, the query leaves even though the connection
    never does. A genuinely air-gapped install should configure a literal IP
    or a container-internal name.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if ip.is_link_local or not (ip.is_loopback or ip.is_private):
            return False
    return True


def host_is_allowed(
    host: str,
    allowed_hosts: Iterable[str],
    *,
    allow_private_network: bool = False,
) -> bool:
    """May a request to `host` leave this process? The only copy of the answer.

    Both transports and :func:`assert_url_allowed` ask this function. They
    used to each carry the rules inline, which is harmless right up until one
    of the three is edited — and a checker that has drifted always drifts
    towards letting more out, because a refusal gets noticed the same day and
    a permission does not.

    Three rules, in order: an exact host match; a match on a subdomain of an
    allowed host (`foo.googleapis.com` → `googleapis.com`, with the dot
    required, so `evilgoogleapis.com` is not a suffix of anything); and — only
    when the caller asked for it — a destination that resolves entirely
    off-internet.

    Case is folded on both sides here rather than at the call sites, so no
    caller can hand in an un-normalised allowlist and get a quiet False.
    """
    host = host.lower()
    if not host:
        return False
    allowed = {h.lower() for h in allowed_hosts}
    if host in allowed:
        return True
    # Subdomain match: foo.googleapis.com → googleapis.com
    if any(host.endswith("." + a) for a in allowed):
        return True
    return allow_private_network and is_private_destination(host)


class _WhitelistPolicy:
    """Everything the sync and async transports hold in common.

    Mixed in AHEAD of the httpx transport class, so `super().__init__` from
    here still reaches httpx's own constructor and the two subclasses differ
    in exactly one method — the one httpx forces them to override. Anything
    kept separately would be a second place the policy could be changed, and
    only one of the two would be changed.
    """

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *args,
        allow_private_network: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._allowed = {h.lower() for h in allowed_hosts}
        self._allow_private = allow_private_network

    def _refuse_unless_allowed(self, request: httpx.Request) -> None:
        """Raise :class:`EgressBlockedError` unless this request may go out.

        The log line and the message live here rather than in each transport
        so an operator grepping `egress_blocked` sees one shape whether the
        call site was sync or async.
        """
        host = (request.url.host or "").lower()
        if host_is_allowed(host, self._allowed, allow_private_network=self._allow_private):
            return
        logger.error("egress_blocked host=%s url=%s", host, str(request.url))
        raise EgressBlockedError(
            f"Network egress to '{host}' is not in whitelist. "
            f"Allowed: {sorted(self._allowed)}"
        )


class WhitelistTransport(_WhitelistPolicy, httpx.HTTPTransport):
    """httpx transport that checks the host against the whitelist before the request."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._refuse_unless_allowed(request)
        return super().handle_request(request)


class AsyncWhitelistTransport(_WhitelistPolicy, httpx.AsyncHTTPTransport):
    """The async twin, which decides nothing of its own.

    httpx sends an `AsyncClient`'s requests to `handle_async_request`, a method
    `httpx.HTTPTransport` does not have, so the async half needs a class of its
    own. Only the dispatch differs: the allowlist, the private-network rule
    and the refusal all come from :class:`_WhitelistPolicy`, so there is no
    such thing here as "the async rules".
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._refuse_unless_allowed(request)
        return await super().handle_async_request(request)


#: httpx client keywords that would route requests AROUND the whitelist
#: transport, so these factories refuse them instead of building a client that
#: looks guarded and is not. `mounts` is the subtle one: it is a per-scheme
#: transport map that WINS over `transport`, so a single `mounts={"all://":
#: httpx.HTTPTransport()}` silently turns the whitelist off.
_ROUTES_AROUND_THE_WHITELIST = ("transport", "mounts")


def _refuse_kwargs_that_route_around_the_whitelist(
    factory: str, client_kwargs: dict[str, Any],
) -> None:
    """Shared by both factories: same two names, same refusal, same wording."""
    for name in _ROUTES_AROUND_THE_WHITELIST:
        if name in client_kwargs:
            raise TypeError(
                f"{factory}() refuses {name}=: it replaces the "
                f"whitelist transport, and a client that looks guarded and "
                f"is not is worse than an obviously raw one."
            )


def build_http_client(
    allowed_hosts: Iterable[str],
    timeout: float | httpx.Timeout = 60.0,
    *,
    allow_private_network: bool = False,
    **client_kwargs,
) -> httpx.Client:
    """Create an httpx Client with the whitelist transport.

    Use this client for ALL outbound HTTP calls. Application code normally
    reaches it through :func:`src.http.build_client`, which fills
    `allowed_hosts` and `allow_private_network` in from Settings so no caller
    has to remember to.

    The Google SDK / Qdrant client have their own HTTP clients — we control
    their egress with a host list via pf / Little Snitch at the OS level. See
    :data:`src.deployment.STRICT_EGRESS_CAVEAT` for what that leaves unproven.

    `allow_private_network` additionally permits destinations that resolve
    entirely to loopback/RFC1918 (never link-local). Off by default so every
    existing caller keeps the behaviour it had.

    `client_kwargs` are handed to `httpx.Client` untouched — `headers`,
    `base_url`, `auth`, `follow_redirects`, everything the real call sites
    need — EXCEPT the two that would replace the transport we just installed.
    Forwarding rather than re-implementing is the point: there is exactly one
    `httpx.Client(...)` in this codebase and it is the line below — its async
    counterpart is the one line in :func:`build_async_http_client`, and the
    guard test counts both.
    """
    _refuse_kwargs_that_route_around_the_whitelist("build_http_client", client_kwargs)
    transport = WhitelistTransport(
        allowed_hosts=allowed_hosts, allow_private_network=allow_private_network,
    )
    return httpx.Client(transport=transport, timeout=timeout, **client_kwargs)


def build_async_http_client(
    allowed_hosts: Iterable[str],
    timeout: float | httpx.Timeout = 60.0,
    *,
    allow_private_network: bool = False,
    **client_kwargs,
) -> httpx.AsyncClient:
    """:func:`build_http_client` for `await`ing call sites.

    Same arguments, same allowlist, same refusal — the differences are the two
    httpx types and nothing else. It is a plain function, not a coroutine:
    constructing an `AsyncClient` does no I/O, and making the caller `await`
    the factory would only invite building one per request.

    Nothing in `src/` calls this yet. It exists so that the first async caller
    has a door to use instead of a reason to write `httpx.AsyncClient(...)` at
    the call site — which the guard test in
    tests/security/test_every_http_client_goes_through_the_factory.py refuses.

    Remember `aclose()`. A sync client leaks a connection pool when it is
    dropped; an async one leaks the pool AND leaves the event loop with tasks
    it never agreed to own.
    """
    _refuse_kwargs_that_route_around_the_whitelist("build_async_http_client", client_kwargs)
    transport = AsyncWhitelistTransport(
        allowed_hosts=allowed_hosts, allow_private_network=allow_private_network,
    )
    return httpx.AsyncClient(transport=transport, timeout=timeout, **client_kwargs)


def assert_url_allowed(
    url: str, allowed_hosts: Iterable[str], *, allow_private_network: bool = False,
) -> None:
    """Helper: checks a single URL without creating a client. Raises EgressBlockedError."""
    # Materialised before the check because `allowed_hosts` may be a generator
    # and the refusal below has to name the same hosts the check just read.
    allowed = {h.lower() for h in allowed_hosts}
    host = (urlparse(url).hostname or "").lower()
    if host_is_allowed(host, allowed, allow_private_network=allow_private_network):
        return
    raise EgressBlockedError(f"URL host '{host}' not in whitelist: {sorted(allowed)}")
