"""Only the reverse proxy may face the internet.

Found while designing the agent sandbox, and it turned out not to be a sandbox
problem at all. `docker-compose.yml` published Postgres as
`"${POSTGRES_HOST_PORT:-5432}:5432"`, and a bare `host:container` mapping binds
to 0.0.0.0. Verified from off-box, not inferred — a TCP connect from outside
the deployment's network reached the database directly. The api and web
containers were published the same way, so they answered from outside too,
even though both sit behind Caddy and had no business being reachable except
through it.

Every tenant's projects, chats, messages and findings live behind that Postgres
socket, with no TLS in front of it. Whether an attacker gets in came down to
one password and Postgres having no unauthenticated CVE that week.

This is also what made the planned agent sandbox's isolation meaningless: a
sandboxed container reaches published ports through the host gateway however
its own network is arranged, so "give the sandbox its own bridge" defends
nothing while the ports face 0.0.0.0.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text()

#: The reverse proxy is the front door, and it is now IN the base file.
#:
#: It used to live in an overlay, and this set was empty because nothing in the
#: base file was allowed to face the internet. That changed when the published
#: web image stopped baking an absolute API URL: the browser bundle asks for
#: the relative `/backend`, which needs something to resolve it, so the proxy
#: became part of the product rather than one deployment's opinion. Without it
#: in the base file the documented quickstart 404s on every page while the API
#: is perfectly healthy.
#:
#: The invariant this file is named for is unchanged, and only now literally
#: expressible: ONLY the proxy may face the internet.
PROXY_OVERLAY = ROOT / "deploy" / "oracle" / "caddy-http.yml"
PUBLIC_SERVICES: set[str] = {"caddy"}


def _port_lines() -> list[tuple[str, str]]:
    """[(service, mapping)] for every published port in the compose file."""
    out: list[tuple[str, str]] = []
    service = ""
    in_ports = False
    for raw in COMPOSE.splitlines():
        stripped = raw.strip()
        if re.match(r"^[a-z0-9_-]+:$", stripped) and raw.startswith("  ") \
                and not raw.startswith("    "):
            service = stripped.rstrip(":")
            in_ports = False
            continue
        if stripped == "ports:":
            in_ports = True
            continue
        if in_ports:
            if stripped.startswith("- "):
                out.append((service, stripped[2:].strip().strip('"').strip("'")))
                continue
            if stripped and not stripped.startswith("#"):
                in_ports = False
    return out


def test_nothing_but_the_proxy_binds_to_every_interface():
    """A bare `host:container` mapping binds 0.0.0.0. Docker also punches its
    own iptables rules, so a host firewall does not save you here."""
    exposed = []
    for service, mapping in _port_lines():
        if service in PUBLIC_SERVICES:
            continue
        # Bound to an explicit address? Then it is deliberate.
        if mapping.startswith("127.0.0.1:") or mapping.startswith("localhost:"):
            continue
        exposed.append(f"{service}: {mapping}")
    assert not exposed, (
        "these bind to 0.0.0.0 and answer from the open internet: "
        f"{exposed}. Prefix with 127.0.0.1: or drop the mapping — services "
        "reach each other by name on the compose network."
    )


def test_postgres_specifically_is_on_loopback():
    """Named because it is the one that holds every tenant's data, and because
    a regression here is the most expensive one on the list."""
    mappings = [m for s, m in _port_lines() if s == "postgres"]
    assert mappings, "the postgres ports block moved — re-check this"
    for mapping in mappings:
        assert mapping.startswith("127.0.0.1:"), (
            f"postgres publishes {mapping!r} to every interface"
        )


def test_the_app_ports_stay_behind_the_proxy():
    """Publishing api/web directly creates a second door past Caddy: no rate
    limiting, no access log, and nowhere to terminate TLS later."""
    for service in ("api", "web"):
        for mapping in [m for s, m in _port_lines() if s == service]:
            assert mapping.startswith("127.0.0.1:"), (
                f"{service} publishes {mapping!r} — Caddy already proxies to it "
                f"by service name"
            )


def test_the_front_door_is_still_open():
    """Negative control. A sweep that bound everything to loopback would pass
    every test above and take the product offline — the proxy is the one thing
    that MUST answer from outside, and it lives in the overlay the deploy
    layers on top."""
    assert PROXY_OVERLAY.is_file(), (
        f"{PROXY_OVERLAY.name} is gone — the deploy composes it in, so its "
        f"absence means nothing serves port 80"
    )
    overlay = PROXY_OVERLAY.read_text()
    assert '- "80:80"' in overlay, "the proxy no longer publishes port 80"


def test_the_base_file_publishes_only_the_front_door():
    """Exactly one service may be reachable, and it must be the proxy.

    This asserted that the base file published NOTHING, which was right while
    the proxy lived in an overlay. It does not any more — and the replacement
    is stricter, not looser: before, an overlay could add anything and this
    file would never see it.
    """
    # Published-on-loopback is not published to the world: `127.0.0.1:8000`
    # is how somebody works on this locally, and the neighbouring test already
    # treats an explicit address as deliberate. The question here is who is
    # REACHABLE, which is the set that binds 0.0.0.0.
    world_facing = {
        service for service, mapping in _port_lines()
        if not mapping.startswith(("127.0.0.1:", "localhost:"))
    }

    assert world_facing == {"caddy"}, (
        f"only the reverse proxy may face the internet; found "
        f"{sorted(world_facing)}"
    )
