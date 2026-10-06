"""A Jira site address is checked before an API token is sent to it.

The token is a password. Whoever can type the address can point the server at
it, so the address must be an https Atlassian Cloud site (or a host the server
operator listed), must name no credentials, path or query, and must resolve to
public addresses only; an operator-listed host may be private but never
link-local (cloud metadata). The host is allowed for the one client built from
the workspace's own connection, never for the whole process.
"""

from __future__ import annotations

import pytest

from src.sync import jira_instance as ji

PUBLIC = "104.192.136.1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("JIRA_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(ji, "_resolve", lambda host, port: [PUBLIC])
    monkeypatch.setattr(ji, "allowed_hosts", lambda: [])


@pytest.mark.parametrize("raw", [
    "https://acme.atlassian.net",
    "https://acme.atlassian.net/",
    "HTTPS://ACME.atlassian.net",
    "https://acme.jira.com",
])
def test_an_atlassian_cloud_site_is_accepted_and_written_the_one_way(raw):
    assert ji.normalise_base_url(raw) == f"https://{raw.split('//')[1].lower().rstrip('/')}"


@pytest.mark.parametrize(("raw", "why"), [
    ("http://acme.atlassian.net", "https://"),
    ("https://user:pw@acme.atlassian.net", "user:password@"),
    ("https://acme.atlassian.net?x=1", "query"),
    ("https://acme.atlassian.net#frag", "fragment"),
    ("https://acme.atlassian.net/jira/software/projects/AIR", "site address"),
    ("https://evil.example.com", "not an Atlassian Cloud site"),
    ("https://atlassian.net.evil.example.com", "not an Atlassian Cloud site"),
    ("https://evil.com/.atlassian.net", "not an Atlassian Cloud site"),
    ("https://a.b.atlassian.net", "not an Atlassian Cloud site"),
    ("https://127.0.0.1", "not an Atlassian Cloud site"),
    ("https://acme.atlassian.net\\@evil.com", "whitespace or control"),
    ("", "empty"),
    ("   ", "empty"),
])
def test_an_address_that_could_send_the_token_elsewhere_is_refused(raw, why):
    with pytest.raises(ji.UnsafeJiraURL, match=why):
        ji.normalise_base_url(raw)


def test_a_refusal_never_repeats_a_password_it_was_given():
    with pytest.raises(ji.UnsafeJiraURL) as err:
        ji.normalise_base_url("https://bot:hunter2-secret@acme.atlassian.net")
    assert "hunter2" not in str(err.value)


def test_a_host_the_operator_listed_is_accepted(monkeypatch):
    monkeypatch.setattr(ji, "allowed_hosts", lambda: ["jira.corp.example"])
    assert ji.normalise_base_url("https://jira.corp.example") == "https://jira.corp.example"


@pytest.mark.parametrize("address", ["10.0.0.5", "192.168.1.9", "127.0.0.1", "169.254.169.254",
                                     "::1", "fd00::1"])
def test_a_cloud_name_that_resolves_to_a_private_address_is_refused(monkeypatch, address):
    monkeypatch.setattr(ji, "_resolve", lambda host, port: [address])
    with pytest.raises(ji.UnsafeJiraURL, match="non-public"):
        ji.validate_base_url("https://acme.atlassian.net")


def test_one_private_answer_among_public_ones_is_still_refused(monkeypatch):
    monkeypatch.setattr(ji, "_resolve", lambda host, port: [PUBLIC, "10.0.0.5"])
    with pytest.raises(ji.UnsafeJiraURL):
        ji.validate_base_url("https://acme.atlassian.net")


def test_an_operator_listed_host_may_be_private_but_never_link_local(monkeypatch):
    monkeypatch.setattr(ji, "allowed_hosts", lambda: ["jira.corp.example"])
    monkeypatch.setattr(ji, "_resolve", lambda host, port: ["10.0.0.5"])
    assert ji.validate_base_url("https://jira.corp.example").host == "jira.corp.example"
    monkeypatch.setattr(ji, "_resolve", lambda host, port: ["169.254.169.254"])
    with pytest.raises(ji.UnsafeJiraURL):
        ji.validate_base_url("https://jira.corp.example")


def test_a_host_that_does_not_resolve_is_a_sentence_not_a_crash(monkeypatch):
    def nothing(host, port):
        raise OSError("nxdomain")

    monkeypatch.setattr(ji, "_resolve", nothing)
    with pytest.raises(ji.UnsafeJiraURL, match="does not resolve"):
        ji.validate_base_url("https://acme.atlassian.net")


def test_a_client_may_reach_only_its_own_site_and_is_pinned_to_the_address_checked():
    kwargs = ji.JiraInstance("https://acme.atlassian.net").http_kwargs()
    assert kwargs["extra_allowed_hosts"] == ("acme.atlassian.net",)
    assert kwargs["pinned_addresses"] == {"acme.atlassian.net": PUBLIC}


def test_the_site_owns_its_own_links_and_no_others():
    site = ji.JiraInstance("https://acme.atlassian.net")
    assert site.owns_url("https://acme.atlassian.net/browse/PROJ-1")
    assert not site.owns_url("https://other.atlassian.net/browse/PROJ-1")
    assert not site.owns_url("http://acme.atlassian.net/browse/PROJ-1")
    assert not site.owns_url("https://acme.atlassian.net@evil.com/browse/PROJ-1")


def test_a_stored_row_with_a_bad_site_raises_instead_of_falling_back_to_a_default():
    with pytest.raises(ji.UnsafeJiraURL):
        ji.instance_from_metadata({"jira_base_url": "https://evil.example.com"})
    with pytest.raises(ji.UnsafeJiraURL):
        ji.instance_from_metadata({})


def test_the_process_wide_allowlist_is_not_widened_by_a_jira_site():
    from src.security.egress import host_is_allowed

    ji.JiraInstance("https://acme.atlassian.net").http_kwargs()
    assert not host_is_allowed("acme.atlassian.net", [], allow_private_network=False)
