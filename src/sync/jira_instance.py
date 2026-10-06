"""Which Jira a workspace talks to — validated before a token is sent to it.

A Jira connection carries the site address next to the API token it belongs
to (credential row `jira`, metadata key :data:`METADATA_KEY`). The save
endpoint takes both together, so nobody who never saw the stored token can
re-point it at a host they control and collect it — the same rule as the
self-hosted GitLab (src/sync/gitlab_instance.py), and this module follows
that one closely.

URL rules (:func:`normalise_base_url`)
--------------------------------------
* https only. There is no http escape hatch: an API token over clear text is
  never acceptable here.
* No user:password@, no query string, no fragment, no path (a Jira under a
  context path is a Data Center habit this does not support: paste the site
  root).
* The host must be an Atlassian Cloud site (``*.atlassian.net``,
  ``*.jira.com``) or be listed in ``JIRA_ALLOWED_HOSTS`` (operator setting).
  A free-form host would let any workspace admin point the server at any
  address of the internet, with the token attached.

Address rules (:func:`check_addresses`)
---------------------------------------
The host is resolved and every address must be globally routable (the LiteLLM
proxy's classifier, :func:`src.llm.litellm_proxy.address_is_blocked`). A host
the operator listed in ``JIRA_ALLOWED_HOSTS`` may resolve to a private
address (a Jira on the LAN / VPN) — never link-local (cloud metadata),
multicast or unspecified. ``EGRESS_ALLOW_PRIVATE_NETWORK`` does not open
this path. The check runs at save time and again every time a client is
built, and the result is pinned (the guarded transport connects to the
validated IP with SNI / Host kept), so a DNS answer that changes in between
cannot redirect the token.

Egress
------
The Jira host is NEVER added to the process-wide allowlist. It is passed as
``extra_allowed_hosts`` to the one client built from that workspace's own
credential row; workspace B never builds a client that may reach workspace
A's site.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from src.sync.gitlab_instance import _resolve, _v4_first

logger = logging.getLogger(__name__)

#: Credential-metadata key holding the normalised site URL.
METADATA_KEY: Final[str] = "jira_base_url"
#: Host suffixes of Atlassian Cloud sites. A site name is one DNS label.
CLOUD_SUFFIXES: Final[tuple[str, ...]] = (".atlassian.net", ".jira.com")

_MAX_URL = 2048
_SITE_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class UnsafeJiraURL(ValueError):
    """The Jira URL fails the rules in the module doc.

    Messages name the host at most — never a token, never a URL with
    credentials — because they reach HTTP responses and logs.
    """


def _settings():
    from src.config import get_settings

    return get_settings()


def allowed_hosts() -> list[str]:
    """``JIRA_ALLOWED_HOSTS``: non-cloud hosts the operator accepts."""
    return [h.strip().lower().rstrip(".")
            for h in (getattr(_settings(), "jira_allowed_hosts", None) or [])
            if h and h.strip()]


def is_cloud_host(host: str) -> bool:
    """``<site>.atlassian.net`` / ``<site>.jira.com``, the site being one
    plain DNS label."""
    for suffix in CLOUD_SUFFIXES:
        if host.endswith(suffix):
            return bool(_SITE_LABEL.match(host[: -len(suffix)]))
    return False


def private_allowed(host: str) -> bool:
    """Is ``host`` on the operator's list (so it may resolve privately)?"""
    from src.security.egress import host_is_allowed

    hosts = allowed_hosts()
    return bool(hosts) and host_is_allowed(host, hosts, allow_private_network=False)


def normalise_base_url(raw: object) -> str:
    """The site root, e.g. ``https://acme.atlassian.net``.

    Raises :class:`UnsafeJiraURL`. Pure: no DNS (see :func:`validate_base_url`).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeJiraURL("the Jira URL is empty")
    text = raw.strip()
    if len(text) > _MAX_URL:
        raise UnsafeJiraURL("the Jira URL is too long")
    if any(c.isspace() or ord(c) < 0x20 for c in text) or "\\" in text:
        raise UnsafeJiraURL("the Jira URL contains whitespace or control characters")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise UnsafeJiraURL("the Jira URL is not a valid URL") from exc
    if parts.scheme.lower() != "https":
        raise UnsafeJiraURL("the Jira URL must start with https://")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UnsafeJiraURL("the Jira URL must not contain user:password@")
    if parts.query or "?" in text:
        raise UnsafeJiraURL("the Jira URL must not contain a query string")
    if parts.fragment or "#" in text:
        raise UnsafeJiraURL("the Jira URL must not contain a fragment")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeJiraURL("the Jira URL has no host")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise UnsafeJiraURL("the Jira host must be ASCII (use its punycode form)") from exc
    if not (is_cloud_host(host) or private_allowed(host)):
        raise UnsafeJiraURL(
            f"'{host}' is not an Atlassian Cloud site (https://<site>.atlassian.net) — "
            "a self-hosted Jira needs its host in JIRA_ALLOWED_HOSTS (set by the "
            "server operator)"
        )
    path = parts.path.rstrip("/")
    if path:
        raise UnsafeJiraURL(
            "the Jira URL must be the site address (e.g. https://acme.atlassian.net), "
            "not a project or page URL"
        )
    netloc = host
    if port is not None and port != 443:
        netloc = f"{host}:{port}"
    return f"https://{netloc}"


def check_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve ``host`` and refuse any address Celmis must not send a token to.

    Returns the addresses, IPv4 first. Raises :class:`UnsafeJiraURL`.
    """
    from src.llm.litellm_proxy import address_is_blocked

    allowlisted = private_allowed(host)
    try:
        addresses = _resolve(host, port)
    except (OSError, UnicodeError) as exc:
        raise UnsafeJiraURL(
            f"the Jira host '{host}' does not resolve from the Celmis server") from exc
    if not addresses:
        raise UnsafeJiraURL(
            f"the Jira host '{host}' does not resolve from the Celmis server")
    blocked = [a for a in addresses if address_is_blocked(a, allowlisted=allowlisted)]
    if blocked:
        raise UnsafeJiraURL(
            f"the Jira host '{host}' resolves to a non-public address "
            f"({', '.join(blocked[:3])})"
        )
    return tuple(_v4_first(addresses))


@dataclass(frozen=True)
class JiraInstance:
    """A normalised Jira site root and everything derived from it."""

    base_url: str

    @property
    def _parts(self):
        return urlsplit(self.base_url)

    @property
    def host(self) -> str:
        return (self._parts.hostname or "").lower()

    @property
    def port(self) -> int:
        return self._parts.port or 443

    @property
    def api_base(self) -> str:
        return f"{self.base_url}/rest/api/3"

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"

    def owns_url(self, url: str) -> bool:
        """Is ``url`` an https link to this site (same host and port)?"""
        try:
            parts = urlsplit(str(url).strip())
            port = parts.port
        except ValueError:
            return False
        return (
            parts.scheme.lower() == "https"
            and not parts.username and not parts.password
            and (parts.hostname or "").lower().rstrip(".") == self.host
            and (port or 443) == self.port
        )

    def addresses(self) -> tuple[str, ...]:
        """Resolve + check now."""
        return check_addresses(self.host, self.port)

    def http_kwargs(self) -> dict[str, Any]:
        """``build_client`` keywords that let a client reach THIS site only:
        the host as the one extra allowed destination and the freshly
        validated address pinned. Raises :class:`UnsafeJiraURL` when the host
        no longer passes the address rules."""
        addrs = self.addresses()
        return {
            "extra_allowed_hosts": (self.host,),
            "pinned_addresses": {self.host: addrs[0]},
        }


def instance_of(base_url: object) -> JiraInstance:
    """A :class:`JiraInstance` for a stored / typed value. No DNS."""
    return JiraInstance(normalise_base_url(base_url))


def validate_base_url(raw: object) -> JiraInstance:
    """Normalise AND resolve — what a save does before it writes anything."""
    instance = instance_of(raw)
    instance.addresses()
    return instance


def instance_from_metadata(metadata: Mapping[str, Any] | None) -> JiraInstance:
    """The site a stored Jira credential belongs to. A row whose stored value
    no longer normalises RAISES: there is no default site to fall back to."""
    raw = (metadata or {}).get(METADATA_KEY) if isinstance(metadata, Mapping) else None
    return instance_of(raw)


__all__ = [
    "CLOUD_SUFFIXES",
    "METADATA_KEY",
    "JiraInstance",
    "UnsafeJiraURL",
    "check_addresses",
    "instance_from_metadata",
    "instance_of",
    "is_cloud_host",
    "normalise_base_url",
    "validate_base_url",
]
