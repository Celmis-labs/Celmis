"""Refuse HTTP that originates on the sandbox's own network.

WHY THIS EXISTS. api and the sandbox share a Docker network because api has
to reach the sandbox, and a Docker network carries traffic both ways. The
compose file has always said so. What it also said — "every api route requires
a signed token this container never receives" — was very nearly true and not
quite: `/healthz` answers 200 to anybody, with the review configuration in it.

Untrusted code runs in that sandbox. It is the code under review.

WHAT THIS IS NOT. It is not the authentication; every route still requires its
token. It narrows an already-authenticated surface by one unauthenticated
route and removes a place to knock. Outbound internet from the sandbox is a
separate, deliberate decision with a stated reason (`pip install`, `npm ci`)
and is untouched here; sandbox→host is dropped at the firewall by the deploy
script, which is the only layer that can tell the host apart from the internet
it is routing for.

THE PEER ADDRESS, NEVER A HEADER. `X-Forwarded-For` is written by the caller,
and here the caller is the party being guarded against. Reading it would let
the sandbox exempt itself, and would let anyone else claim to be the sandbox —
turning a boundary into a denial-of-service knob.
"""

from __future__ import annotations

import ipaddress
import logging
import os

logger = logging.getLogger(__name__)

#: Compose pins the sandbox network's subnet and passes it here. Docker hands
#: out a different one per host otherwise, and a guard aimed at the wrong
#: network guards nothing while looking installed.
ENV_VAR = "SANDBOX_NET_SUBNET"

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network


def denied_networks(raw: str | None) -> tuple[_Net, ...]:
    """Parse the setting. Comma-separated CIDRs, or bare addresses.

    Blank denies nothing, deliberately: an operator who has never heard of
    this variable must not end up with an instance that answers nothing. A
    malformed entry is skipped with a warning rather than taking the process
    down — one typo in an optional hardening setting is not worth an outage,
    and the entries around it still apply.
    """
    out: list[_Net] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(ipaddress.ip_network(chunk, strict=False))
        except ValueError:
            logger.warning("sandbox_guard_bad_subnet value=%r — ignored", chunk[:60])
    return tuple(out)


def from_environment() -> tuple[_Net, ...]:
    return denied_networks(os.environ.get(ENV_VAR))


class SandboxNetworkGuard:
    """Pure ASGI so it runs BEFORE routing.

    A route-level dependency would leave `/healthz` — the one route that has
    no dependencies — exactly as open as it was.
    """

    _BODY = (b'{"detail":"This API is not reachable from the execution '
             b'sandbox."}')

    def __init__(self, app, networks: tuple[_Net, ...]):
        self.app = app
        self.networks = networks

    def _is_sandbox(self, host: str) -> bool:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(addr in net for net in self.networks)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.networks:
            return await self.app(scope, receive, send)

        client = scope.get("client") or ()
        host = client[0] if client else None
        if host and self._is_sandbox(str(host)):
            logger.warning("sandbox_guard_blocked path=%s", scope.get("path", "")[:120])
            await send({
                "type": "http.response.start", "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(self._BODY)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": self._BODY})
            return

        return await self.app(scope, receive, send)
