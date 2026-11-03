"""Which peers may tell uvicorn the real client address, scheme and host.

``uvicorn --forwarded-allow-ips`` decides whose ``X-Forwarded-For`` /
``-Proto`` / ``-Host`` are believed. The default (127.0.0.1) believes nobody
behind a container proxy, so the API saw every request as plain http from the
proxy's address: redirects dropped to http and the rate limiter counted one
client. ``*`` would fix that and let ANY client forge the headers whenever the
API is reachable without the proxy.

In the bundled compose file the proxy is a container on the project's default
network, whose address Docker chooses and may change. So the answer is that
network - the one the API shares with web and the proxy, and the only one a
published port reaches the API through - minus the sandbox network, whose
containers run the code under review and must never be believed.

``python -m src.api.proxy_trust`` prints a comma-separated list for
``FORWARDED_ALLOW_IPS``; it prints nothing when the networks cannot be read
(uvicorn then keeps its safe default). An operator who knows better sets
``CELMIS_FORWARDED_ALLOW_IPS`` and this is not consulted.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import struct
from pathlib import Path


def connected_networks(route_file: str = "/proc/net/route") -> list[ipaddress.IPv4Network]:
    """Directly attached IPv4 networks (no gateway) from the kernel table."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        lines = Path(route_file).read_text().splitlines()[1:]
    except OSError:
        return nets
    for line in lines:
        cols = line.split()
        if len(cols) < 8:
            continue
        dest_hex, gateway_hex, mask_hex = cols[1], cols[2], cols[7]
        if int(gateway_hex, 16) != 0 or int(dest_hex, 16) == 0:
            continue  # the default route, or one that goes through a router
        dest = socket.inet_ntoa(struct.pack("<L", int(dest_hex, 16)))
        mask = socket.inet_ntoa(struct.pack("<L", int(mask_hex, 16)))
        try:
            nets.append(ipaddress.IPv4Network(f"{dest}/{mask}", strict=False))
        except ValueError:
            continue
    return nets


def trusted_networks(
    sandbox_subnet: str | None = None, route_file: str = "/proc/net/route",
) -> list[ipaddress.IPv4Network]:
    sandbox: ipaddress.IPv4Network | None = None
    if sandbox_subnet and sandbox_subnet.strip():
        try:
            sandbox = ipaddress.IPv4Network(sandbox_subnet.strip(), strict=False)
        except ValueError:
            sandbox = None
    out = []
    for net in connected_networks(route_file):
        if sandbox is not None and (net.overlaps(sandbox)):
            continue
        # Never a network wider than a /16: a mask like /8 means this is not
        # a container bridge and believing all of it would be believing a LAN.
        if net.prefixlen < 16 or not net.is_private:
            continue
        out.append(net)
    return out


def main() -> None:
    nets = trusted_networks(os.environ.get("SANDBOX_NET_SUBNET"))
    print(",".join(str(n) for n in nets))


if __name__ == "__main__":
    main()
