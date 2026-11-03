"""Which peers uvicorn believes about client address and scheme.

`*` would let any client forge X-Forwarded-For when the API is reachable
without the proxy; the default believes nobody and breaks redirects and the
rate limiter. The bundled compose file trusts the project's own network and
never the sandbox's.
"""

from __future__ import annotations

import ipaddress

from src.api import proxy_trust

# /proc/net/route on a container with two networks and a default route.
ROUTE = """Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT
eth0\t00000000\t010011AC\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
eth1\t00B21CAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
"""


def _route(tmp_path):
    f = tmp_path / "route"
    f.write_text(ROUTE)
    return str(f)


def test_the_connected_networks_are_read_without_the_default_route(tmp_path):
    nets = proxy_trust.connected_networks(_route(tmp_path))
    assert [str(n) for n in nets] == ["172.17.0.0/16", "172.28.178.0/24"]


def test_the_sandbox_network_is_never_trusted(tmp_path):
    nets = proxy_trust.trusted_networks("172.28.178.0/24", _route(tmp_path))
    assert [str(n) for n in nets] == ["172.17.0.0/16"]
    sandbox_peer = ipaddress.ip_address("172.28.178.5")
    assert not any(sandbox_peer in n for n in nets)


def test_an_unreadable_table_trusts_nobody(tmp_path):
    assert proxy_trust.trusted_networks("", str(tmp_path / "missing")) == []


def test_the_bundled_compose_sets_it_for_the_proxy_hop():
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert "--forwarded-allow-ips" in text
    assert "CELMIS_FORWARDED_ALLOW_IPS" in text
    assert "--forwarded-allow-ips '*'" not in text and '--forwarded-allow-ips "*"' not in text
    assert 'CELMIS_TRUST_PROXY: "${CELMIS_TRUST_PROXY:-1}"' in text
