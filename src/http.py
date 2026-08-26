"""The one door out of this process for HTTP.

Every outbound call in `src/` is supposed to be built here. The reason is a
measurement, not a preference: before this module there were 27 places that
wrote ``httpx.Client(...)`` by hand and exactly two that went through the
egress whitelist, and one of those two was the local-embeddings probe — the
single call that never leaves the customer's own network anyway. The
allowlist in :mod:`src.security.egress` was therefore guarding nothing that
mattered, while the docs implied it guarded everything.

This module holds no policy of its own. It answers one question — *what are
the allowed hosts for this process* — by reading Settings, and hands the rest
to :mod:`src.security.egress`, which owns the transports for both the sync and
the async half. Splitting it that way keeps the security decision in one file
and the convenience in another; a caller that needs different policy is a
caller that should be arguing with :mod:`src.security.egress`, not writing its
own client.

What a guarded client actually promises
---------------------------------------
It refuses a request whose host is not on ``egress_allowed_hosts`` (or a
subdomain of one), and — unless ``egress_allow_private_network`` is on —
refuses loopback/RFC1918 too. It does NOT promise the process sends nothing
else: third-party SDKs carry their own HTTP stacks and this transport cannot
see them. :data:`src.deployment.STRICT_EGRESS_CAVEAT` names them one by one.
The only complete answer is still a firewall.

Destinations the operator configured
------------------------------------
Some hosts are legitimate and can never be on a shipped allowlist: the
LiteLLM proxy at ``http://litellm:4000``, a self-hosted GitLab, a model
server on loopback. Those callers pass ``extra_allowed_hosts`` — one host,
named at the call site, taken from the configuration that already decided
where to connect. That is deliberately not the same thing as
``egress_allow_private_network``, which opens the whole LAN to every client
at once.

Async
-----
:func:`build_async_client` is the same door for ``await``ing callers, and
:class:`src.security.egress.AsyncWhitelistTransport` is the transport behind
it. Both halves share one policy object, so "allowed" means the same thing on
either side by construction rather than by two people remembering.

Nothing in `src/` calls it today — the scan behind
``tests/security/test_every_http_client_goes_through_the_factory`` still finds
zero ``httpx.AsyncClient(`` outside these two files. It was built ahead of the
first caller because the alternative is that the first caller writes
``httpx.AsyncClient(...)`` at the call site under deadline, and adding a
factory afterwards means converting rather than importing.

If you are that first caller, check three things. The client must be closed
with ``await client.aclose()`` — an abandoned async client leaks its pool and
leaves the event loop holding tasks it never agreed to own; prefer
``async with``. Nothing here is thread-safe across event loops, so build the
client inside the loop that will use it rather than at import. And if you find
yourself wanting a per-scheme mount or a custom transport, that is the
allowlist you are about to switch off — argue with
:mod:`src.security.egress`, do not pass the kwarg.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import httpx

from src.security.egress import (
    EgressBlockedError,
    build_async_http_client,
    build_http_client,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "EgressBlockedError",
    "allowed_hosts",
    "build_async_client",
    "build_client",
]

#: Same default `build_http_client` has always used. Every real call site
#: passes its own; this exists so that forgetting to is not a client that
#: hangs forever.
DEFAULT_TIMEOUT = 60.0


def allowed_hosts(extra: Iterable[str] = ()) -> list[str]:
    """The hosts a client built here may reach: Settings plus ``extra``.

    Settings is read on every call rather than cached at import: `get_settings`
    is itself cached, and a module-level snapshot would freeze the allowlist
    of whichever test imported this first.
    """
    from src.config import get_settings

    hosts = list(get_settings().egress_allowed_hosts)
    hosts.extend(h for h in extra if h)
    return hosts


def _client_kwargs(
    headers: Mapping[str, str] | None,
    base_url: str,
    auth: Any,
    follow_redirects: bool,
) -> dict[str, Any]:
    """The httpx keywords both factories forward, assembled in one place.

    Falsy values are dropped instead of forwarded so httpx's own defaults stay
    in force — the promise that makes a conversion a rename is that
    ``build_client(...)`` behaves like ``httpx.Client(...)`` at the shapes the
    call sites use, and passing ``base_url=""`` explicitly is not obviously the
    same as not passing it at all.

    Sync and async share this for the same reason they share the transport
    policy: an argument honoured by one factory and quietly ignored by the
    other is a difference nobody discovers until it is a bug report.
    """
    kwargs: dict[str, Any] = {"follow_redirects": follow_redirects}
    if headers:
        kwargs["headers"] = dict(headers)
    if base_url:
        kwargs["base_url"] = base_url
    if auth is not None:
        kwargs["auth"] = auth
    return kwargs


def build_client(
    *,
    timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
    base_url: str = "",
    auth: Any = None,
    follow_redirects: bool = False,
    extra_allowed_hosts: Iterable[str] = (),
) -> httpx.Client:
    """An ``httpx.Client`` that can only reach the hosts this install allows.

    Drop-in for ``httpx.Client(...)`` at the shapes the call sites in `src/`
    actually use. Defaults match httpx's own, so converting a call site is a
    rename and nothing else — `follow_redirects` in particular stays False,
    because a client that follows a redirect it did not ask to follow is how
    an allowlisted host becomes a jump to one that is not.

    `extra_allowed_hosts` names destinations that came from this
    installation's configuration (a proxy URL, a self-hosted API base). Pass
    the host, not the URL, and pass it from the same config value the request
    is built from — a host taken from user input would make the allowlist
    decorative.

    Raises :class:`src.security.egress.EgressBlockedError` at request time,
    never at construction time: the check needs the URL.
    """
    from src.config import get_settings

    settings = get_settings()
    return build_http_client(
        allowed_hosts(extra_allowed_hosts),
        timeout=timeout,
        allow_private_network=settings.egress_allow_private_network,
        **_client_kwargs(headers, base_url, auth, follow_redirects),
    )


def build_async_client(
    *,
    timeout: float | httpx.Timeout = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
    base_url: str = "",
    auth: Any = None,
    follow_redirects: bool = False,
    extra_allowed_hosts: Iterable[str] = (),
) -> httpx.AsyncClient:
    """:func:`build_client` for ``await``ing callers — same door, same policy.

    Argument for argument the sync factory, because the moment the two
    signatures differ, converting a call site from one to the other stops
    being a rename and someone reaches for ``httpx.AsyncClient(...)`` instead.
    ``transport=`` and ``mounts=`` are absent from this signature exactly as
    they are absent from the sync one: `mounts` is a per-scheme map that
    OUTRANKS `transport`, so a single accepted kwarg would hand back a client
    that looks guarded and is not.

    Not a coroutine — building an ``AsyncClient`` does no I/O, and an
    ``await``ed factory invites one client per request. Close it with
    ``await client.aclose()``, or use ``async with``.

    No caller in `src/` today; see this module's Async section for what the
    first one should check.
    """
    from src.config import get_settings

    settings = get_settings()
    return build_async_http_client(
        allowed_hosts(extra_allowed_hosts),
        timeout=timeout,
        allow_private_network=settings.egress_allow_private_network,
        **_client_kwargs(headers, base_url, auth, follow_redirects),
    )
