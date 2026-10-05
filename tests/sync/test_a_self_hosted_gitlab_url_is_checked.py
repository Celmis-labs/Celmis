"""A self-hosted GitLab URL is an address the server will send a token to.

A workspace admin types it on the Connections page; Celmis then sends that
workspace's GitLab token to it on every API call and every clone. So it gets
the same treatment as a workspace's own LiteLLM proxy: https only, no
credentials or query in it, and a host that resolves to the public internet —
unless the OPERATOR (not the workspace admin) listed it. The cloud metadata
address is refused even then.

The matrix below is the contract. Each refusal is a way somebody could point
the token at a place it must not go.
"""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.sync import gitlab_instance as gi
from src.sync.gitlab_instance import (
    DEFAULT_INSTANCE,
    METADATA_KEY,
    UnsafeGitLabURL,
    instance_from_metadata,
    normalise_base_url,
    validate_base_url,
)

PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    for name in ("GITLAB_ALLOWED_HOSTS", "GITLAB_HTTP_ALLOWED_HOSTS",
                 "GITLAB_CA_BUNDLE", "EGRESS_ALLOW_PRIVATE_NETWORK"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(monkeypatch, **env: str) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


def _resolves_to(monkeypatch, *addresses: str) -> list[tuple[str, int]]:
    asked: list[tuple[str, int]] = []

    def fake(host: str, port: int) -> list[str]:
        asked.append((host, port))
        return list(addresses)

    monkeypatch.setattr(gi, "_resolve", fake)
    return asked


# ─── normalisation ───────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("https://gitlab.example.com", "https://gitlab.example.com"),
    ("https://gitlab.example.com/", "https://gitlab.example.com"),
    ("  https://GitLab.Example.COM/  ", "https://gitlab.example.com"),
    ("https://gitlab.example.com/api/v4", "https://gitlab.example.com"),
    ("https://gitlab.example.com/api/v4/", "https://gitlab.example.com"),
    ("https://gitlab.example.com:443/", "https://gitlab.example.com"),
    ("https://gitlab.example.com:8443", "https://gitlab.example.com:8443"),
    # GitLab under a sub-path (relative_url_root) keeps it.
    ("https://example.com/gitlab", "https://example.com/gitlab"),
    ("https://example.com/gitlab/", "https://example.com/gitlab"),
    ("https://example.com/gitlab/api/v4", "https://example.com/gitlab"),
    ("https://example.com/tools/gitlab", "https://example.com/tools/gitlab"),
    ("https://gitlab.com", "https://gitlab.com"),
])
def test_the_stored_value_is_the_instance_root(raw, expected):
    assert normalise_base_url(raw) == expected


@pytest.mark.parametrize("raw, why", [
    ("", "empty"),
    ("   ", "empty"),
    (None, "empty"),
    ("gitlab.example.com", "https"),
    ("ftp://gitlab.example.com", "https"),
    ("file:///etc/passwd", "https"),
    ("http://gitlab.example.com", "GITLAB_HTTP_ALLOWED_HOSTS"),
    ("https://user:pass@gitlab.example.com", "user:password"),
    ("https://glpat-abc@gitlab.example.com", "user:password"),
    ("https://gitlab.example.com/?x=1", "query"),
    ("https://gitlab.example.com/?", "query"),
    ("https://gitlab.example.com/#frag", "fragment"),
    ("https://gitlab.example.com/group/proj/-/merge_requests/1", "instance address"),
    ("https://gitlab.example.com/a/../b", "instance address"),
    ("https://gitlab.example.com/a b", "whitespace"),
    ("https://gitlab.example.com\\@evil.com", "whitespace"),
    ("https://", "no host"),
    ("https://gitlab.example.com:99999", "not a valid URL"),
    ("https://gitläb.example.com", "ASCII"),
])
def test_an_unsafe_url_is_refused_before_any_lookup(monkeypatch, raw, why):
    asked = _resolves_to(monkeypatch, PUBLIC)
    # (An empty value on the save path means gitlab.com — see
    # test_a_row_without_the_key_is_gitlab_com — so emptiness is a
    # normalisation question only.)
    check = normalise_base_url if raw in (None, "") else validate_base_url
    with pytest.raises(UnsafeGitLabURL) as err:
        check(raw)
    assert why.lower() in str(err.value).lower()
    assert not asked, "a refused URL must not even be resolved"


def test_http_is_allowed_only_for_an_exact_listed_host(monkeypatch):
    _settings(monkeypatch, GITLAB_HTTP_ALLOWED_HOSTS='["gitlab.lan.test"]')
    assert normalise_base_url("http://gitlab.lan.test/gitlab") == "http://gitlab.lan.test/gitlab"
    assert normalise_base_url("http://gitlab.lan.test:80") == "http://gitlab.lan.test"
    with pytest.raises(UnsafeGitLabURL):
        normalise_base_url("http://sub.gitlab.lan.test")
    with pytest.raises(UnsafeGitLabURL):
        normalise_base_url("http://other.test")


# ─── addresses ───────────────────────────────────────────────────────


@pytest.mark.parametrize("address", [
    "10.0.0.5", "172.16.3.4", "192.168.1.10", "127.0.0.1", "0.0.0.0",
    "169.254.169.254",            # cloud metadata
    "100.64.0.1",                 # CGNAT
    "::1", "fd00::1", "fe80::1",
    "::ffff:10.0.0.1",            # IPv4-mapped private
    "64:ff9b::a00:1",             # NAT64 of 10.0.0.1
    "224.0.0.1",
])
def test_a_non_public_address_is_refused(monkeypatch, address):
    _resolves_to(monkeypatch, address)
    with pytest.raises(UnsafeGitLabURL, match="non-public"):
        validate_base_url("https://gitlab.example.com")


def test_one_private_answer_among_public_ones_is_enough_to_refuse(monkeypatch):
    _resolves_to(monkeypatch, PUBLIC, "10.0.0.5")
    with pytest.raises(UnsafeGitLabURL, match="10.0.0.5"):
        validate_base_url("https://gitlab.example.com")


def test_a_public_instance_passes(monkeypatch):
    asked = _resolves_to(monkeypatch, PUBLIC)
    inst = validate_base_url("https://gitlab.example.com/gitlab/")
    assert inst.base_url == "https://gitlab.example.com/gitlab"
    assert inst.api_base == "https://gitlab.example.com/gitlab/api/v4"
    assert asked == [("gitlab.example.com", 443)]


def test_a_name_that_does_not_resolve_is_refused(monkeypatch):
    def boom(host, port):
        raise OSError("nodename nor servname provided")

    monkeypatch.setattr(gi, "_resolve", boom)
    with pytest.raises(UnsafeGitLabURL, match="does not resolve"):
        validate_base_url("https://gitlab.internal.example.com")


def test_the_operator_allowlist_admits_a_private_instance(monkeypatch):
    _settings(monkeypatch, GITLAB_ALLOWED_HOSTS='["corp.test"]')
    _resolves_to(monkeypatch, "10.1.2.3")
    assert validate_base_url("https://gitlab.corp.test").host == "gitlab.corp.test"


@pytest.mark.parametrize("address", ["169.254.169.254", "fe80::1", "224.0.0.1", "0.0.0.0"])
def test_the_allowlist_never_admits_metadata_multicast_or_unspecified(monkeypatch, address):
    _settings(monkeypatch, GITLAB_ALLOWED_HOSTS='["gitlab.corp.test"]')
    _resolves_to(monkeypatch, address)
    with pytest.raises(UnsafeGitLabURL):
        validate_base_url("https://gitlab.corp.test")


def test_the_allowlist_is_not_a_suffix_match(monkeypatch):
    _settings(monkeypatch, GITLAB_ALLOWED_HOSTS='["corp.test"]')
    _resolves_to(monkeypatch, "10.1.2.3")
    with pytest.raises(UnsafeGitLabURL):
        validate_base_url("https://evilcorp.test")


def test_the_egress_private_switch_does_not_open_this_path(monkeypatch):
    """EGRESS_ALLOW_PRIVATE_NETWORK opens the LAN for the operator's own
    services; a workspace admin's URL must not ride on it."""
    _settings(monkeypatch, EGRESS_ALLOW_PRIVATE_NETWORK="true")
    _resolves_to(monkeypatch, "10.1.2.3")
    with pytest.raises(UnsafeGitLabURL):
        validate_base_url("https://gitlab.example.com")


def test_every_client_rechecks_the_address_and_pins_it(monkeypatch):
    """A rebinding DNS server answers public at save time and private later.
    The client is built from a fresh answer, and that answer is what it
    connects to."""
    inst = gi.instance_of("https://gitlab.example.com")
    _resolves_to(monkeypatch, PUBLIC)
    kw = inst.http_kwargs()
    assert kw["extra_allowed_hosts"] == ("gitlab.example.com",)
    assert kw["pinned_addresses"] == {"gitlab.example.com": PUBLIC}

    _resolves_to(monkeypatch, "169.254.169.254")
    with pytest.raises(UnsafeGitLabURL):
        inst.http_kwargs()
    with pytest.raises(UnsafeGitLabURL):
        inst.git_config()


def test_gitlab_com_is_neither_resolved_nor_widened(monkeypatch):
    asked = _resolves_to(monkeypatch, "10.0.0.1")
    assert DEFAULT_INSTANCE.http_kwargs() == {}
    assert DEFAULT_INSTANCE.git_config() == []
    assert not asked


def test_the_ca_bundle_reaches_the_client_and_git(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    _settings(monkeypatch, GITLAB_CA_BUNDLE=str(bundle))
    _resolves_to(monkeypatch, PUBLIC)
    inst = gi.instance_of("https://gitlab.example.com:8443")
    assert inst.http_kwargs()["ca_bundle"] == str(bundle)
    config = dict(inst.git_config())
    assert config["http.sslCAInfo"] == str(bundle)
    assert config["http.curloptResolve"] == f"gitlab.example.com:8443:{PUBLIC}"


def test_there_is_no_switch_that_turns_tls_off():
    import inspect

    from src import http
    from src.security import egress

    for fn in (http.build_client, http.build_async_client,
               egress.build_http_client, egress.build_async_http_client):
        params = inspect.signature(fn).parameters
        assert "verify" not in params, fn.__name__
    from src.config import Settings

    assert not [f for f in Settings.model_fields
                if "gitlab" in f and ("verify" in f or "insecure" in f)]


# ─── what a stored row means ─────────────────────────────────────────


def test_a_row_without_the_key_is_gitlab_com():
    assert instance_from_metadata({}) is DEFAULT_INSTANCE
    assert instance_from_metadata(None) is DEFAULT_INSTANCE
    assert instance_from_metadata({"username": "bot"}) is DEFAULT_INSTANCE


def test_a_corrupt_stored_url_raises_instead_of_falling_back_to_gitlab_com():
    """Falling back would send a self-hosted token to gitlab.com."""
    with pytest.raises(UnsafeGitLabURL):
        instance_from_metadata({METADATA_KEY: "https://u:p@gitlab.example.com"})
    with pytest.raises(UnsafeGitLabURL):
        instance_from_metadata({METADATA_KEY: "http://gitlab.example.com"})


# ─── which URLs belong to the instance ───────────────────────────────


@pytest.mark.parametrize("url, rel", [
    ("https://example.com/gitlab/group/proj", "group/proj"),
    ("https://example.com/gitlab/group/sub/proj/-/merge_requests/3", "group/sub/proj/-/merge_requests/3"),
    ("https://EXAMPLE.com:443/gitlab/g/p", "g/p"),
    ("https://example.com/gitlabx/g/p", None),        # not the sub-path
    ("https://example.com/g/p", None),                # outside the sub-path
    ("https://example.com:8443/gitlab/g/p", None),    # other port
    ("http://example.com/gitlab/g/p", None),          # downgraded scheme
    ("https://evil.com/gitlab/g/p", None),
    ("https://u:p@example.com/gitlab/g/p", None),
])
def test_a_url_is_on_the_instance_only_under_its_root(url, rel):
    inst = gi.instance_of("https://example.com/gitlab")
    assert inst.relative_path(url) == rel
