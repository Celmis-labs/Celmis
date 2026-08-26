"""Тести egress whitelist — забороняє всі домени крім allowed."""

from __future__ import annotations

import httpx
import pytest

from src.security.egress import (
    AsyncWhitelistTransport,
    EgressBlockedError,
    WhitelistTransport,
    assert_url_allowed,
    build_async_http_client,
    build_http_client,
)

#: Every test that asks "what does a guarded client do?" runs twice, once per
#: flavour. The sync and async transports share one policy object precisely so
#: they cannot answer differently; parametrising rather than writing a second
#: copy of each test is what keeps that claim checked instead of asserted.
BOTH_FACTORIES = pytest.mark.parametrize(
    "build", [build_http_client, build_async_http_client], ids=["sync", "async"],
)


async def _get(client: httpx.Client | httpx.AsyncClient, url: str) -> httpx.Response:
    """One GET through either flavour of guarded client.

    Written once so a single test body can put the identical question to both.
    The moment sync and async are exercised by two different bodies, they can
    disagree and only one of the bodies notices.
    """
    if isinstance(client, httpx.AsyncClient):
        return await client.get(url)
    return client.get(url)


async def _close(client: httpx.Client | httpx.AsyncClient) -> None:
    """An abandoned async client leaks its pool AND leaves the loop holding
    tasks it never agreed to own, so the tests close what they open."""
    if isinstance(client, httpx.AsyncClient):
        await client.aclose()
    else:
        client.close()


def test_allowed_host_passes() -> None:
    assert_url_allowed(
        "https://generativelanguage.googleapis.com/v1/models",
        allowed_hosts=["generativelanguage.googleapis.com"],
    )


def test_disallowed_host_blocked() -> None:
    with pytest.raises(EgressBlockedError):
        assert_url_allowed(
            "https://evil.example.com/exfil",
            allowed_hosts=["generativelanguage.googleapis.com"],
        )


def test_subdomain_of_allowed_passes() -> None:
    assert_url_allowed(
        "https://api.bitbucket.org/repos",
        allowed_hosts=["bitbucket.org"],
    )


def test_lookalike_domain_blocked() -> None:
    # googleapis.com.evil.com — не повинен проходити
    with pytest.raises(EgressBlockedError):
        assert_url_allowed(
            "https://googleapis.com.evil.com/exfil",
            allowed_hosts=["googleapis.com"],
        )


def test_aiplatform_vertex_regional_allowed() -> None:
    """Vertex AI regional endpoint (us-central1-aiplatform.googleapis.com).
    Префікс через `-`, не `.` — root-level whitelist `googleapis.com` потрібен."""
    assert_url_allowed(
        "https://us-central1-aiplatform.googleapis.com/v1/projects/x/models",
        allowed_hosts=["googleapis.com"],
    )


def test_generativelanguage_allowed() -> None:
    """generativelanguage.googleapis.com — для embeddings + Gemini API key auth."""
    assert_url_allowed(
        "https://generativelanguage.googleapis.com/v1beta/models/embedding-001:embedContent",
        allowed_hosts=["googleapis.com"],
    )


def test_partial_substring_match_blocked() -> None:
    """`evilgenerativelanguage.googleapis.com` — endswith повинен ловити лише
    `.allowed`, не суфікс без точки."""
    with pytest.raises(EgressBlockedError):
        assert_url_allowed(
            "https://evilgenerativelanguage.googleapis.com.attacker.com/x",
            allowed_hosts=["generativelanguage.googleapis.com"],
        )


def test_empty_host_blocked() -> None:
    """URL без хоста (місцеві тощо) — блокується."""
    with pytest.raises(EgressBlockedError):
        assert_url_allowed("file:///etc/passwd", allowed_hosts=["bitbucket.org"])


def test_settings_default_allowlist_includes_v3_hosts() -> None:
    """Перевірка: default settings — root-level domains які покривають
    всі реальні Google + Bitbucket endpoints через subdomain matching."""
    from src.config import get_settings

    settings = get_settings()
    assert "googleapis.com" in settings.egress_allowed_hosts
    assert "bitbucket.org" in settings.egress_allowed_hosts


# ─── Phase 12: e2e egress enforcement ───────────────────────────────


def test_build_http_client_blocks_disallowed_real_request():
    """e2e: httpx Client з WhitelistTransport реально ловить заборонений host
    при первому handle_request. Перевіряє що integration працює, не тільки
    assert_url_allowed standalone."""
    from src.security.egress import build_http_client

    # Не дозволяємо google → запит до github має впасти
    client = build_http_client(allowed_hosts=["bitbucket.org"], timeout=2.0)
    try:
        with pytest.raises(EgressBlockedError, match="not in whitelist"):
            client.get("https://api.github.com/zen")
    finally:
        client.close()


def test_build_http_client_passes_allowed_through_transport(monkeypatch):
    """Whitelist host НЕ blocks — transport дзвонить super().handle_request().

    Не робимо реальний запит (мокаємо super().handle_request) щоб не
    залежати від мережі.
    """
    from unittest.mock import patch as mpatch

    from src.security.egress import WhitelistTransport

    transport = WhitelistTransport(allowed_hosts=["googleapis.com"])
    fake_response = object()
    with mpatch("httpx.HTTPTransport.handle_request", return_value=fake_response) as super_call:
        import httpx
        req = httpx.Request("GET", "https://generativelanguage.googleapis.com/v1/models")
        result = transport.handle_request(req)
        assert result is fake_response
        super_call.assert_called_once()


def test_egress_allowlist_is_exactly_the_documented_set():
    """Мережевий моніторинг має бачити рівно ці цілі — і жодної більше.

    Список ріс разом з провайдерами (github/gitlab додались після Phase 12).
    Сенс тесту не в конкретному числі хостів, а в тому, що додати новий
    egress-напрямок не можна непомітно: список тут і в Settings мають
    змінюватись одним комітом.
    """
    from src.config import get_settings

    expected = {
        "googleapis.com",          # Gemini / embeddings / Vertex
        "bitbucket.org",           # Bitbucket Cloud
        "github.com",              # GitHub API
        "githubusercontent.com",   # raw file fetches
        "gitlab.com",              # GitLab API
        # Dependency audit — version registries and the vulnerability DB.
        # These arrived with the audit feature and belong in the monitored
        # set: an audit reaches out to every one of them on every run.
        "osv.dev",                 # api.osv.dev
        "npmjs.org",               # registry.npmjs.org
        "pypi.org",
        "golang.org",              # proxy.golang.org
        "crates.io",
    }
    settings = get_settings()
    assert set(settings.egress_allowed_hosts) == expected, (
        f"Egress allowlist changed: {settings.egress_allowed_hosts}. "
        f"If that is intentional, update this test in the same commit."
    )


# ─── private network: the air-gap switch ─────────────────────────────
#
# The allowlist names hosts on the INTERNET. Whether a socket may be opened to
# something that never leaves the network is a separate question, and it has to
# be separate: an air-gapped install runs with the allowlist EMPTY and still
# has to reach its own embeddings server.


def test_private_network_is_blocked_by_default():
    """Default-off is what keeps every existing deployment byte-identical —
    and keeps this from being an SSRF hole nobody opted into."""
    with pytest.raises(EgressBlockedError):
        assert_url_allowed("http://127.0.0.1:11434/v1/embeddings", allowed_hosts=[])
    with pytest.raises(EgressBlockedError):
        assert_url_allowed("http://10.0.0.5:8080/v1/embeddings", allowed_hosts=["github.com"])


def test_private_network_passes_when_switched_on():
    assert_url_allowed(
        "http://127.0.0.1:11434/v1/embeddings",
        allowed_hosts=[], allow_private_network=True,
    )


def test_the_switch_does_not_open_the_internet():
    """It opens the LAN. A public host stays refused with an empty allowlist."""
    for url in ("https://api.openai.com/v1/embeddings",
                "https://generativelanguage.googleapis.com/v1beta/models"):
        with pytest.raises(EgressBlockedError):
            assert_url_allowed(url, allowed_hosts=[], allow_private_network=True)


def test_cloud_metadata_is_excluded_from_private():
    """169.254.169.254 answers `is_private == True` in the stdlib, and handing
    it to an SSRF is how instance credentials walk out. Link-local is excluded
    by name, in both IP families."""
    from src.security.egress import is_private_destination

    assert is_private_destination("169.254.169.254") is False
    with pytest.raises(EgressBlockedError):
        assert_url_allowed(
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            allowed_hosts=[], allow_private_network=True,
        )


def test_private_is_decided_by_resolved_address_not_by_name():
    """A hostname is trusted for where it points, not what it is called — so a
    name cannot talk its way onto the LAN, and a compose service name works."""
    from src.security.egress import is_private_destination

    assert is_private_destination("8.8.8.8") is False
    assert is_private_destination("no-such-host.invalid") is False   # unknown ≠ private
    assert is_private_destination("192.168.4.4") is True


def test_default_settings_keep_the_switch_off():
    from src.config import get_settings

    assert get_settings().egress_allow_private_network is False


# ─── the factory every call site is supposed to use ──────────────────
#
# The allowlist was real code guarding almost nothing: 27 places built their
# own httpx.Client and two called build_http_client, one of which was the
# local-embeddings probe that never leaves the customer's network anyway.
# src/http.py is the door those call sites move to; these tests are about the
# door, not about the transport it installs.


@BOTH_FACTORIES
async def test_an_empty_allowlist_permits_nothing_by_accident(build):
    """The air-gapped configuration. Empty must mean empty — not 'unset, so
    allow' — or the one install that most needs the guard has none."""
    client = build(allowed_hosts=[], timeout=2.0)
    try:
        for url in ("https://api.github.com/zen",
                    "https://generativelanguage.googleapis.com/v1/models",
                    "http://127.0.0.1:11434/v1/embeddings"):
            with pytest.raises(EgressBlockedError):
                await _get(client, url)
    finally:
        await _close(client)


@BOTH_FACTORIES
@pytest.mark.parametrize("kwarg", ["transport", "mounts"])
def test_the_factory_refuses_arguments_that_route_around_it(build, kwarg):
    """`mounts` is the quiet one: a per-scheme transport map that WINS over
    `transport`, so it would hand back a client that looks guarded and is not.

    Both factories, because a refusal that only the sync half enforces is the
    same hole wearing an `await`.

    Matching the factory's OWN wording rather than just the kwarg name: with
    the check deleted, `transport=` still raises TypeError — httpx gets the
    keyword twice — so a looser match passed on a factory that had stopped
    refusing anything. `mounts=` in that state builds the unguarded client and
    fails much later, or not at all."""
    with pytest.raises(TypeError, match=f"refuses {kwarg}="):
        build([], **{kwarg: httpx.HTTPTransport()})


@pytest.mark.parametrize(
    "build, transport_cls",
    [(build_http_client, WhitelistTransport),
     (build_async_http_client, AsyncWhitelistTransport)],
    ids=["sync", "async"],
)
async def test_the_factory_forwards_the_shapes_the_call_sites_need(build, transport_cls):
    """The 27 raw clients pass headers, auth, base_url and follow_redirects.
    If the factory cannot take those, converting a call site means changing
    its behaviour, and nobody converts anything. The async factory has no call
    sites to convert yet — it takes the same four so that when it does, the
    conversion is still a rename."""
    client = build(
        ["example.com"], timeout=3.0,
        headers={"User-Agent": "celmis-test"},
        base_url="https://example.com/api",
        auth=("user", "token"),
        follow_redirects=True,
    )
    try:
        assert client.headers["User-Agent"] == "celmis-test"
        # httpx normalises a base_url with a trailing slash — that is httpx's
        # behaviour, unchanged by us, and asserting the exact string here
        # would be testing httpx.
        assert str(client.base_url).rstrip("/") == "https://example.com/api"
        assert client.auth is not None
        assert client.follow_redirects is True
        assert isinstance(client._transport, transport_cls)
    finally:
        await _close(client)


#: The two doors in src/http.py, which differ from the two above by filling
#: the allowlist in from Settings. Every promise the module makes has to hold
#: for both or the async one is a trap for whoever uses it first.
BOTH_DOORS = pytest.mark.parametrize(
    "door", ["build_client", "build_async_client"],
)


def _door(name: str):
    import src.http

    return getattr(src.http, name)


@BOTH_DOORS
async def test_the_door_reads_the_allowlist_from_settings(door):
    from src.config import get_settings
    from src.http import allowed_hosts

    settings = get_settings()
    assert set(settings.egress_allowed_hosts) <= set(allowed_hosts())
    client = _door(door)(timeout=2.0)
    try:
        with pytest.raises(EgressBlockedError):
            await _get(client, "https://evil.example.com/exfil")
    finally:
        await _close(client)


@BOTH_DOORS
async def test_extra_allowed_hosts_opens_exactly_one_more_door(door):
    """A configured destination — a proxy URL, a self-hosted API base — is not
    on any shipped allowlist and is usually on the LAN, where the default
    refuses. It gets named at the call site instead of opening the whole
    private network for every client at once.

    Port 1 on loopback: nothing listens, so a refused CONNECTION is proof the
    egress check let the request through. No network required."""
    build = _door(door)

    guarded = build(timeout=2.0)
    try:
        with pytest.raises(EgressBlockedError):
            await _get(guarded, "http://127.0.0.1:1/health")
    finally:
        await _close(guarded)

    excepted = build(timeout=2.0, extra_allowed_hosts=("127.0.0.1",))
    try:
        with pytest.raises(httpx.ConnectError):
            await _get(excepted, "http://127.0.0.1:1/health")
    finally:
        await _close(excepted)


@BOTH_DOORS
async def test_the_extra_door_is_one_host_not_a_wildcard(door):
    client = _door(door)(timeout=2.0, extra_allowed_hosts=("127.0.0.1",))
    try:
        with pytest.raises(EgressBlockedError):
            await _get(client, "http://10.0.0.5:8080/health")
    finally:
        await _close(client)


@BOTH_DOORS
@pytest.mark.parametrize("kwarg", ["transport", "mounts"])
def test_neither_door_takes_a_kwarg_that_routes_around_it(door, kwarg):
    """src/http.py refuses these by not having them in its signature, which is
    a stronger refusal than a runtime check and an easier one to lose: adding
    `**kwargs` to either door for convenience would forward both silently."""
    with pytest.raises(TypeError, match=kwarg):
        _door(door)(timeout=2.0, **{kwarg: httpx.HTTPTransport()})


# ─── one policy, two transports ──────────────────────────────────────
#
# `src/` has no async HTTP caller today; the async factory exists so the first
# one has a door to use instead of a deadline and an `httpx.AsyncClient(...)`.
# What these tests protect is therefore not a live code path — it is the claim
# that when that caller arrives, the guarded async client already decides
# exactly what the sync one decides. The two transports share one policy
# object to make that true; the parametrisation is what keeps it checked.


#: Each case is (url, allow_private_network, what the client must do about it).
#: None of them touch the network: a refusal happens before the socket, and
#: port 1 on loopback refuses the connection immediately.
PRIVATE_NETWORK_CASES = [
    # The switch is off — the default every existing deployment runs.
    ("http://127.0.0.1:1/health", False, EgressBlockedError),
    ("http://10.0.0.5:8080/health", False, EgressBlockedError),
    # The switch is on. It opens the LAN...
    ("http://127.0.0.1:1/health", True, httpx.ConnectError),
    # ...and nothing else: link-local is excluded by name, because 169.254.169.254
    # is where instance credentials live and `ipaddress` calls it private.
    ("http://169.254.169.254/latest/meta-data/", True, EgressBlockedError),
    # ...and it is not a way onto the internet with an empty allowlist.
    ("https://api.github.com/zen", True, EgressBlockedError),
]


@BOTH_FACTORIES
@pytest.mark.parametrize("url, allow_private, outcome", PRIVATE_NETWORK_CASES)
async def test_the_private_network_rule_is_one_rule_for_both_clients(
    build, url, allow_private, outcome,
):
    """The whole point of sharing the policy, stated as a test.

    If someone gives the async transport its own copy of the rules, this fails
    on the case they got wrong rather than in production on the case nobody
    thought about. `pytest -k private_network` is the diff between the two."""
    client = build(allowed_hosts=[], timeout=2.0, allow_private_network=allow_private)
    try:
        with pytest.raises(outcome):
            await _get(client, url)
    finally:
        await _close(client)


async def test_the_async_transport_lets_an_allowed_host_through():
    """The permitting half — the sync twin of this is
    `test_build_http_client_passes_allowed_through_transport`. Mocked at
    httpx's own method so the test proves delegation without a network."""
    from unittest.mock import AsyncMock
    from unittest.mock import patch as mpatch

    transport = AsyncWhitelistTransport(allowed_hosts=["googleapis.com"])
    fake_response = object()
    with mpatch(
        "httpx.AsyncHTTPTransport.handle_async_request",
        new_callable=AsyncMock, return_value=fake_response,
    ) as super_call:
        req = httpx.Request("GET", "https://generativelanguage.googleapis.com/v1/models")
        result = await transport.handle_async_request(req)
    assert result is fake_response
    super_call.assert_awaited_once()


@BOTH_FACTORIES
async def test_both_clients_refuse_with_the_same_words(build):
    """An operator greps one string. A second phrasing for the async path
    would mean the runbook covers half the refusals."""
    client = build(allowed_hosts=["bitbucket.org"], timeout=2.0)
    try:
        with pytest.raises(EgressBlockedError) as exc:
            await _get(client, "https://api.github.com/zen")
    finally:
        await _close(client)
    assert str(exc.value) == (
        "Network egress to 'api.github.com' is not in whitelist. "
        "Allowed: ['bitbucket.org']"
    )


# ─── the LLM gateway: the call that mattered ─────────────────────────
#
# src/llm/gateway.py is the chat and review path. It was a bare client, which
# is why "the allowlist protects the LLM calls" was untrue.


def test_the_gateway_still_reaches_the_proxy_the_operator_configured(monkeypatch, caplog):
    """Byte-identical for a configured gateway: LITELLM_PROXY_URL is normally
    a compose service name or loopback, so a client that only honoured the
    public allowlist would have turned every gateway call into a silent
    fallback to direct provider keys."""
    from src.llm import gateway

    monkeypatch.setenv("LITELLM_PROXY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test-master-key")
    with caplog.at_level("WARNING", logger="src.llm.gateway"):
        resp = gateway._call("GET", "/health/liveliness")
    assert resp.status == 0            # nothing is listening on port 1
    assert "litellm_gateway_unreachable" in caplog.text
    assert "egress_blocked" not in caplog.text


def test_the_gateway_names_only_its_own_proxy_host(monkeypatch):
    from src.llm import gateway

    monkeypatch.setenv("LITELLM_PROXY_URL", "http://litellm:4000/")
    assert gateway._proxy_host() == ("litellm",)
    monkeypatch.delenv("LITELLM_PROXY_URL", raising=False)
    assert gateway._proxy_host() == ()


def test_a_blocked_proxy_is_logged_as_a_configuration_answer(monkeypatch, caplog):
    """An egress refusal is not a network condition: retrying never fixes it,
    so it must not be filed under 'unreachable' where an operator reads it as
    a flaky proxy."""
    from src.llm import gateway

    monkeypatch.setenv("LITELLM_PROXY_URL", "https://evil.example.com")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test-master-key")
    monkeypatch.setattr(gateway, "_proxy_host", lambda: ())
    with caplog.at_level("WARNING", logger="src.llm.gateway"):
        resp = gateway._call("GET", "/health/liveliness")
    assert resp.status == 0
    assert "litellm_gateway_egress_blocked" in caplog.text
