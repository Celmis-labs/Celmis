"""The sandbox could reach the API, and /healthz answered it.

docker-compose.yml states the residual in its own words:

    a shared network is reachable in both directions at the IP level, so the
    sandbox can address api. Every api route requires a signed token this
    container never receives.

The first sentence is true and unavoidable — api has to reach the sandbox, and
a Docker network carries traffic both ways. The second sentence was not quite
true: `/healthz` answers 200 to anyone, and what it answers with is the review
configuration — model names, timeouts, whether S3 and Redis are wired.

Measured from inside the sandbox on a deployed instance, running as the code
under review would:

    api:8000        -> OPEN      GET /healthz -> 200 {"status":"ok",...}
    postgres:5432   -> gaierror
    qdrant:6333     -> gaierror
    172.17.0.1:22   -> OPEN      (the docker host)
    example.com     -> OPEN      (deliberate: pip install / npm ci)

Outbound internet is a documented design decision with a stated reason and
stays. The other two do not: this file closes the API surface, and the deploy
script drops sandbox→host traffic at the firewall.

THE CHECK USES THE PEER ADDRESS, never a header. A guard that reads
X-Forwarded-For is one the caller writes for you, and the caller here is the
thing being guarded against.
"""
from __future__ import annotations

import pytest

from src.api.sandbox_guard import SandboxNetworkGuard, denied_networks

SUBNET = "172.28.90.0/24"


async def _call(guard, client_ip: str | None, headers=None):
    sent: list[dict] = []
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"status":"ok"}'})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "path": "/healthz",
             "headers": headers or [],
             "client": (client_ip, 51234) if client_ip else None}
    await guard(app)(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, reached


def _guard():
    nets = denied_networks(SUBNET)
    assert nets, "the subnet did not parse"
    return lambda app: SandboxNetworkGuard(app, nets)


@pytest.mark.asyncio
async def test_a_request_from_the_sandbox_network_is_refused():
    status, _, reached = await _call(_guard(), "172.28.90.7")
    assert status == 403
    assert reached == [], "the request reached the route before being refused"


@pytest.mark.asyncio
async def test_healthz_is_refused_too():
    """The one route that answered without a token is the one that mattered."""
    status, body, _ = await _call(_guard(), "172.28.90.3")
    assert status == 403
    assert b"review_settings" not in body


@pytest.mark.asyncio
async def test_a_request_from_anywhere_else_is_untouched():
    status, body, reached = await _call(_guard(), "172.19.0.4")
    assert status == 200
    assert body == b'{"status":"ok"}'
    assert reached == ["/healthz"]


@pytest.mark.asyncio
async def test_a_forged_forwarding_header_does_not_get_the_sandbox_out():
    """The guarded party writes its own headers."""
    status, _, _ = await _call(
        _guard(), "172.28.90.9",
        headers=[(b"x-forwarded-for", b"8.8.8.8"), (b"host", b"celmis.example.com")],
    )
    assert status == 403


@pytest.mark.asyncio
async def test_a_forged_forwarding_header_does_not_get_anyone_else_in():
    """...and it must not be usable to impersonate the sandbox either.

    Denying on a header would hand every caller a way to be refused, which is
    a denial-of-service knob rather than a boundary.
    """
    status, _, _ = await _call(
        _guard(), "203.0.113.5",
        headers=[(b"x-forwarded-for", b"172.28.90.2")],
    )
    assert status == 200


@pytest.mark.asyncio
async def test_a_request_with_no_peer_address_is_not_a_crash():
    """ASGI allows `client` to be None — a unix socket, some test harnesses."""
    status, _, _ = await _call(_guard(), None)
    assert status == 200


@pytest.mark.asyncio
async def test_a_non_http_scope_passes_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await SandboxNetworkGuard(app, denied_networks(SUBNET))(
        {"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


# ─── parsing the setting ─────────────────────────────────────────────

def test_several_subnets_can_be_named():
    nets = denied_networks("172.28.90.0/24, 10.9.0.0/16")
    assert len(nets) == 2


def test_a_blank_setting_denies_nothing():
    """Absent configuration must not take the API off the air.

    Fail-closed here would mean an operator who never heard of this variable
    gets a dead instance; the guard is a narrowing of an already-authenticated
    surface, not the authentication itself.
    """
    assert denied_networks("") == ()
    assert denied_networks(None) == ()


def test_a_malformed_subnet_is_skipped_not_fatal():
    nets = denied_networks("not-a-subnet, 172.28.90.0/24")
    assert len(nets) == 1


def test_a_bare_address_counts_as_itself():
    nets = denied_networks("172.28.90.4")
    assert nets and nets[0].num_addresses == 1


# ─── and that it is wired ────────────────────────────────────────────

def test_the_guard_is_installed_on_the_app(monkeypatch):
    """The tests above prove the class, not that anything uses it.

    An earlier version of this test grepped main.py for the class name and
    passed with the `add_middleware` call deleted — the import line still
    carried the word. Build the app and look at its middleware stack instead.
    """
    monkeypatch.setenv("SANDBOX_NET_SUBNET", SUBNET)

    from src.api.main import build_app
    from src.api.sandbox_guard import SandboxNetworkGuard

    app = build_app()
    installed = [m.cls for m in app.user_middleware]
    assert SandboxNetworkGuard in installed, (
        "the guard is never added to the application: the sandbox can still "
        f"reach every unauthenticated route. installed={installed}"
    )
    assert installed[0] is SandboxNetworkGuard, (
        "the guard is not outermost — Starlette runs user_middleware in "
        "registration order from the outside in, and a guard that runs after "
        "routing cannot close /healthz"
    )


def test_the_guard_stays_out_of_the_way_when_unconfigured(monkeypatch):
    """No subnet means no middleware at all, not a middleware denying nothing."""
    monkeypatch.delenv("SANDBOX_NET_SUBNET", raising=False)

    from src.api.main import build_app
    from src.api.sandbox_guard import SandboxNetworkGuard

    app = build_app()
    assert SandboxNetworkGuard not in [m.cls for m in app.user_middleware]


def test_compose_pins_the_subnet_and_tells_the_api_which_one():
    """The middleware needs a CIDR it can trust; Docker picks a fresh one per
    host unless told, and a guard aimed at the wrong network guards nothing."""
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert "SANDBOX_NET_SUBNET" in compose
    assert "ipam" in compose, "sandbox_net still takes whatever subnet Docker hands it"
