"""The address a document or a 401 names is the one the client used.

Behind the bundled proxy the API sees `http://api:8000`: no TLS, no `/backend`
prefix. A discovery document built from that named an address no client can
reach, and a client that followed it ended on the web app's sign-in page.
`src.mcp_server.public_url` is the one place that works the public base out;
these tests pin where each part of it comes from, and what it refuses to echo.
"""

from __future__ import annotations

import logging

import pytest

from src.mcp_server import public_url

#: What Caddy sends for a request to https://celmis.example.com/backend/...
PROXIED = {
    "host": "celmis.example.com",
    "x-forwarded-host": "celmis.example.com",
    "x-forwarded-proto": "https",
    "x-forwarded-for": "203.0.113.7",
}


@pytest.fixture(autouse=True)
def _no_ambient_address(monkeypatch):
    from src.config import get_settings

    for name in ("PUBLIC_BASE_URL", "CELMIS_PUBLIC_BASE_URL", "MCP_ALLOWED_HOSTS", "MCP_OAUTH_ISSUER"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    public_url._PLAIN_HTTP_NOTED.clear()
    yield
    get_settings.cache_clear()
    public_url._PLAIN_HTTP_NOTED.clear()


def _declare(monkeypatch, public: str = "https://celmis.example.com", hosts: str = ""):
    from src.config import get_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", public)
    if hosts:
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", hosts)
    get_settings.cache_clear()


def _base(headers: dict, scheme: str = "http", netloc: str = "api:8000") -> str:
    return public_url.public_base(headers, scheme, netloc)


def test_the_proxy_headers_give_the_public_https_address_with_the_prefix(monkeypatch):
    _declare(monkeypatch)
    sent = {**PROXIED, "x-forwarded-prefix": "/backend"}
    assert _base(sent) == "https://celmis.example.com/backend"


def test_the_prefix_is_inferred_for_a_proxied_request_on_the_configured_host(monkeypatch):
    """An install whose proxy predates `X-Forwarded-Prefix` still gets it right."""
    _declare(monkeypatch)
    assert _base(PROXIED) == "https://celmis.example.com/backend"


def test_a_prefix_the_proxy_sent_empty_means_the_api_is_at_the_root(monkeypatch):
    """A separate API domain: the proxy says there is no prefix, and there is none."""
    _declare(monkeypatch, hosts="api.celmis.example.com")
    sent = {**PROXIED, "host": "api.celmis.example.com", "x-forwarded-host": "api.celmis.example.com",
            "x-forwarded-prefix": ""}
    assert _base(sent) == "https://api.celmis.example.com"


def test_a_proxy_that_reports_plain_http_does_not_downgrade_the_configured_https_host(monkeypatch):
    """The bundled `:80` proxy trusts nobody, so behind a TLS terminator it sends
    `X-Forwarded-Proto: http` for a client that used https."""
    _declare(monkeypatch)
    sent = {**PROXIED, "x-forwarded-proto": "http", "x-forwarded-prefix": "/backend"}
    assert _base(sent) == "https://celmis.example.com/backend"
    assert _base({k: v for k, v in sent.items() if k != "x-forwarded-proto"}) == (
        "https://celmis.example.com/backend")


def test_a_host_declared_only_in_the_allow_list_keeps_the_scheme_the_proxy_reported(monkeypatch):
    _declare(monkeypatch, hosts="lab.celmis.example.com")
    sent = {**PROXIED, "host": "lab.celmis.example.com", "x-forwarded-host": "lab.celmis.example.com",
            "x-forwarded-proto": "http"}
    assert _base(sent).startswith("http://lab.celmis.example.com")


def test_a_plain_http_install_stays_plain_http(monkeypatch):
    _declare(monkeypatch, public="http://203.0.113.9")
    sent = {"host": "203.0.113.9", "x-forwarded-host": "203.0.113.9", "x-forwarded-proto": "http",
            "x-forwarded-for": "198.51.100.2"}
    assert _base(sent) == "http://203.0.113.9/backend"


def test_an_api_domain_other_than_the_app_host_has_no_inferred_prefix(monkeypatch):
    _declare(monkeypatch, hosts="api.celmis.example.com")
    sent = {**PROXIED, "host": "api.celmis.example.com", "x-forwarded-host": "api.celmis.example.com"}
    assert _base(sent) == "https://api.celmis.example.com"


def test_a_directly_reached_api_names_itself_without_a_prefix(monkeypatch):
    """No proxy in front (a developer machine): the request's own address, as is."""
    assert _base({"host": "localhost:8000"}, "http", "localhost:8000") == "http://localhost:8000"


def test_the_explicit_setting_wins_over_everything_the_request_says(monkeypatch):
    _declare(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://mcp.celmis.example.com/edge/")
    sent = {**PROXIED, "x-forwarded-prefix": "/backend"}
    assert _base(sent) == "https://mcp.celmis.example.com/edge"


@pytest.mark.parametrize("bad", ["celmis.example.com", "ftp://celmis.example.com", "https://",
                                 "https://celmis.example.com/x?y=1", "javascript:alert(1)",
                                 'https://celmis.example.com/a"b', "https://celmis.example.com/a b",
                                 "https://celmis.example.com/a/../b", "https://celmis.example.com//x",
                                 "https://celmis.example.com/%2e%2e", "https://celmis.example.com/" + "a" * 80])
def test_an_unusable_explicit_setting_is_ignored_not_trusted(monkeypatch, caplog, bad):
    _declare(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ISSUER", bad)
    with caplog.at_level(logging.WARNING, logger=public_url.logger.name):
        assert _base({**PROXIED, "x-forwarded-prefix": "/backend"}) == "https://celmis.example.com/backend"
    assert "mcp_oauth_issuer_ignored" in caplog.text


def test_an_explicit_setting_cannot_carry_a_quote_into_the_challenge_header(monkeypatch):
    """`resource_metadata="..."` is a quoted header parameter: the value that goes in
    it is checked for what it is allowed to contain, not just for its host."""
    _declare(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ISSUER", 'https://celmis.example.com/a"b')
    url = public_url.protected_resource_metadata_url(_base({**PROXIED, "x-forwarded-prefix": "/backend"}), "/mcp")
    assert '"' not in url


@pytest.mark.parametrize("good, expected", [
    ("https://mcp.celmis.example.com/edge/v1/", "https://mcp.celmis.example.com/edge/v1"),
    ("https://celmis.example.com", "https://celmis.example.com"),
    ("http://203.0.113.9:8080/backend", "http://203.0.113.9:8080/backend"),
])
def test_a_usable_explicit_setting_is_used_as_written_minus_the_trailing_slash(monkeypatch, caplog, good, expected):
    _declare(monkeypatch)
    monkeypatch.setenv("MCP_OAUTH_ISSUER", good)
    with caplog.at_level(logging.WARNING, logger=public_url.logger.name):
        assert _base(PROXIED) == expected
    assert "mcp_oauth_issuer_ignored" not in caplog.text


def test_a_host_nobody_declared_is_not_reflected_into_a_document(monkeypatch):
    """The client's `Host` is attacker-controlled. A document must not carry it
    back unless the MCP endpoint itself would answer at that host."""
    _declare(monkeypatch)
    sent = {**PROXIED, "host": "evil.example.net", "x-forwarded-host": "evil.example.net"}
    base = _base(sent)
    assert "evil" not in base
    assert base.startswith("https://celmis.example.com")


def test_a_host_the_operator_declared_is_used(monkeypatch):
    _declare(monkeypatch, hosts="mcp.celmis.example.com")
    sent = {**PROXIED, "host": "mcp.celmis.example.com", "x-forwarded-host": "mcp.celmis.example.com"}
    assert _base(sent).startswith("https://mcp.celmis.example.com")


def test_the_scheme_is_the_one_the_client_used_and_nothing_else(monkeypatch):
    _declare(monkeypatch)
    for proto in ("javascript", "gopher", "https://x", ""):
        base = _base({**PROXIED, "x-forwarded-proto": proto, "x-forwarded-prefix": "/backend"})
        assert base.split("://")[0] in ("http", "https"), (proto, base)
    # A local, plain-http address: the scheme the client used is kept.
    assert _base({"host": "localhost:8000", "x-forwarded-host": "localhost:8000",
                  "x-forwarded-proto": "http"}).startswith("http://localhost:8000")


@pytest.mark.parametrize("bad", ["/../admin", "/a/../b", "//evil.example.net", "/a b", "/%2e%2e", "/a?x=1",
                                 "/a#f", "https://evil.example.net", "/" + "a" * 80, "/.", "/.."])
def test_a_prefix_that_is_not_a_plain_path_is_ignored(monkeypatch, bad):
    """A prefix goes into a URL the client will fetch: no traversal, no host, no query."""
    _declare(monkeypatch)
    base = _base({**PROXIED, "x-forwarded-prefix": bad})
    assert base == "https://celmis.example.com/backend", (bad, base)  # the inferred convention


def test_only_the_first_value_of_a_forwarded_list_counts(monkeypatch):
    _declare(monkeypatch)
    sent = {**PROXIED, "x-forwarded-host": "celmis.example.com, evil.example.net",
            "x-forwarded-proto": "https, http", "x-forwarded-prefix": "/backend, /other"}
    assert _base(sent) == "https://celmis.example.com/backend"


@pytest.mark.parametrize("host", ["bad host", "a/b", "a@b", "a:b:c", "ex ample.com", "", "-a.example.com", "a" * 300])
def test_a_syntactically_invalid_host_is_never_written_into_a_url(monkeypatch, host):
    base = _base({"host": host, "x-forwarded-host": host, "x-forwarded-proto": "https"}, "http", "localhost:8000")
    assert " " not in base and "@" not in base and "/a/b" not in base
    assert base.startswith(("http://", "https://"))


def test_the_metadata_pointer_sits_under_the_apis_own_public_base():
    """Not at the host root: that belongs to the web app, which does not know it."""
    base = "https://celmis.example.com/backend"
    assert public_url.protected_resource_metadata_url(base, "/mcp/dev") == (
        "https://celmis.example.com/backend/.well-known/oauth-protected-resource/mcp/dev")
    assert public_url.protected_resource_metadata_url(base, "/mcp/") == (
        "https://celmis.example.com/backend/.well-known/oauth-protected-resource/mcp")


def test_a_raw_asgi_scope_gives_the_same_answer_as_the_headers(monkeypatch):
    _declare(monkeypatch)
    scope = {
        "type": "http", "scheme": "http", "server": ("api", 8000),
        "headers": [(k.encode(), v.encode()) for k, v in {**PROXIED, "x-forwarded-prefix": "/backend"}.items()],
    }
    assert public_url.base_from_scope(scope) == "https://celmis.example.com/backend"


# ── a TLS terminator in front of the bundled `:80` proxy ─────────────────────

#: What the API receives there: the terminator said https, then Caddy overwrote
#: the header with the plain connection it sees itself.
BEHIND_TERMINATOR = {**PROXIED, "x-forwarded-proto": "http", "x-forwarded-prefix": "/backend"}


def test_a_host_declared_only_in_the_allow_list_behind_a_tls_terminator_is_named_http_and_the_log_says_what_to_set(
        monkeypatch, caplog):
    """The proxy cannot see the terminator, and nothing else says `https` for this host:
    the answer stays what the proxy reported (a plain-http install looks identical),
    and the operator is told once, with the lines to write."""
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "celmis.example.com")
    with caplog.at_level(logging.WARNING, logger=public_url.logger.name):
        assert _base(BEHIND_TERMINATOR) == "http://celmis.example.com/backend"
        _base(BEHIND_TERMINATOR)
    notes = [r.getMessage() for r in caplog.records if "mcp_public_base_plain_http" in r.getMessage()]
    assert len(notes) == 1, notes
    assert "PUBLIC_BASE_URL=https://celmis.example.com" in notes[0]
    assert "MCP_OAUTH_ISSUER=https://celmis.example.com/backend" in notes[0]


def test_writing_https_in_public_base_url_fixes_that_topology_and_silences_the_log(monkeypatch, caplog):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "celmis.example.com")
    _declare(monkeypatch, public="https://celmis.example.com", hosts="celmis.example.com")
    with caplog.at_level(logging.WARNING, logger=public_url.logger.name):
        assert _base(BEHIND_TERMINATOR) == "https://celmis.example.com/backend"
    assert "mcp_public_base_plain_http" not in caplog.text


def test_the_explicit_setting_fixes_that_topology_too(monkeypatch, caplog):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "celmis.example.com")
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://celmis.example.com/backend")
    with caplog.at_level(logging.WARNING, logger=public_url.logger.name):
        assert _base(BEHIND_TERMINATOR) == "https://celmis.example.com/backend"
    assert "mcp_public_base_plain_http" not in caplog.text


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000", "203.0.113.9"])
def test_a_local_or_bare_ip_address_over_plain_http_is_not_worth_a_warning(monkeypatch, caplog, host):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", host)
    with caplog.at_level(logging.WARNING, logger=public_url.logger.name):
        _base({"host": host, "x-forwarded-host": host, "x-forwarded-proto": "http"})
    assert "mcp_public_base_plain_http" not in caplog.text


def test_with_nothing_declared_the_caller_is_handed_back_the_host_it_sent(monkeypatch):
    """The documented limit of "a host nobody declared is not reflected": it holds when
    `PUBLIC_BASE_URL` is set (see the test above), not on an install nobody has configured
    yet, where the MCP endpoint itself answers 421 and only the caller sees this answer."""
    sent = {"host": "evil.example.net", "x-forwarded-host": "evil.example.net", "x-forwarded-proto": "https",
            "x-forwarded-prefix": "/backend"}
    assert _base(sent) == "https://evil.example.net/backend"
    _declare(monkeypatch)
    assert "evil" not in _base(sent)
