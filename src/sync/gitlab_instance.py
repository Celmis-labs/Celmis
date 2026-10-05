"""Which GitLab a workspace talks to — gitlab.com or its own instance.

A GitLab connection carries an optional base URL (Connections page, workspace
admin only). Absent, it is ``https://gitlab.com`` and nothing changes for the
installs that never set it. Present, EVERY GitLab call made for that
workspace — REST v4, git clone/fetch, webhook install, MR listing, review
comments, approvals — goes to that instance and to no other.

Storage
-------
The normalised base URL lives in the credential row's metadata under
:data:`METADATA_KEY`, next to the token it belongs to. One row, one instance:
the base URL can only change together with the token (the save endpoint takes
both), so an admin who never saw a stored token cannot re-point it at a host
they control and collect it. No migration — the metadata column already
exists.

URL rules (:func:`normalise_base_url`)
--------------------------------------
* https only. Plain http:// only for a host listed EXACTLY in
  ``GITLAB_HTTP_ALLOWED_HOSTS`` (operator setting; the token then crosses the
  network in clear text, which is why it is never a default).
* No user:password@, no query string, no fragment.
* A trailing ``/`` and ``/api/v4`` are normalised away: the stored value is
  the instance ROOT, which may sit under a sub-path
  (``https://host/gitlab``).
* The path is plain segments only — a pasted project URL (``/-/``) is not a
  base URL.

Address rules (:func:`check_addresses`)
---------------------------------------
The host is resolved and every address must be globally routable — the same
classifier the LiteLLM proxy uses (:func:`src.llm.litellm_proxy.address_is_blocked`):
private, loopback, link-local (169.254.169.254 — cloud metadata), CGNAT,
ULA, multicast, reserved and IPv4-embedded-in-IPv6 forms are refused.
Operator escape hatch for an instance on the LAN/VPN: ``GITLAB_ALLOWED_HOSTS``
(JSON list, exact host or subdomain). A listed host may resolve to private
addresses — never link-local, multicast or unspecified.
``EGRESS_ALLOW_PRIVATE_NETWORK`` does NOT open this path.

The check runs at save time AND again every time a client or a git process is
built for the instance; the result is pinned (httpx: the guarded transport
connects to the validated IP with SNI/Host kept as the hostname; git:
``http.curloptResolve``), so a DNS answer that changes between the check and
the connection — DNS rebinding — cannot redirect the token.

Egress
------
gitlab.com is on the shipped ``EGRESS_ALLOWED_HOSTS``. A self-hosted host is
NOT added to any process-wide list: it is passed as ``extra_allowed_hosts``
to the one client built for the workspace that configured it, derived from
that workspace's own credential row. Workspace B never builds a client that
may reach workspace A's instance.

TLS
---
``GITLAB_CA_BUNDLE`` (a PEM path) is ADDED to the public roots for
self-hosted GitLab calls and clones. There is no switch that disables
certificate verification, and none will be added.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL: Final[str] = "https://gitlab.com"
DEFAULT_HOST: Final[str] = "gitlab.com"
#: Credential-metadata key holding the normalised base URL.
METADATA_KEY: Final[str] = "gitlab_base_url"
API_SUFFIX: Final[str] = "/api/v4"

_MAX_URL = 2048
#: One plain path segment of a base URL: what GitLab's relative_url_root can be.
_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")


class UnsafeGitLabURL(ValueError):
    """The GitLab URL fails the rules in the module doc.

    Messages name the host at most — never a token, never a full URL with
    credentials — because they reach HTTP responses and logs.
    """


# ─── settings ────────────────────────────────────────────────────────


def _settings():
    from src.config import get_settings

    return get_settings()


def _listed(host: str, hosts: list[str] | None) -> bool:
    from src.security.egress import host_is_allowed

    hosts = [h for h in (hosts or []) if h]
    return bool(hosts) and host_is_allowed(host, hosts, allow_private_network=False)


def private_allowed(host: str) -> bool:
    """``GITLAB_ALLOWED_HOSTS``: may this host resolve to a private address?"""
    return _listed(host, list(getattr(_settings(), "gitlab_allowed_hosts", None) or []))


def http_allowed(host: str) -> bool:
    """``GITLAB_HTTP_ALLOWED_HOSTS``: exact host match only — no subdomains."""
    hosts = {h.strip().lower().rstrip(".")
             for h in (getattr(_settings(), "gitlab_http_allowed_hosts", None) or [])
             if h and h.strip()}
    return host.lower() in hosts


def ca_bundle() -> str | None:
    value = str(getattr(_settings(), "gitlab_ca_bundle", "") or "").strip()
    return value or None


# ─── URL rules ───────────────────────────────────────────────────────


def normalise_base_url(raw: object) -> str:
    """The instance root, e.g. ``https://gitlab.example.com/gitlab``.

    Raises :class:`UnsafeGitLabURL`. Pure: no DNS (see :func:`validate_base_url`).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeGitLabURL("the GitLab URL is empty")
    text = raw.strip()
    if len(text) > _MAX_URL:
        raise UnsafeGitLabURL("the GitLab URL is too long")
    if any(c.isspace() or ord(c) < 0x20 for c in text) or "\\" in text:
        raise UnsafeGitLabURL("the GitLab URL contains whitespace or control characters")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise UnsafeGitLabURL("the GitLab URL is not a valid URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise UnsafeGitLabURL("the GitLab URL must start with https://")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UnsafeGitLabURL("the GitLab URL must not contain user:password@")
    if parts.query or "?" in text:
        raise UnsafeGitLabURL("the GitLab URL must not contain a query string")
    if parts.fragment or "#" in text:
        raise UnsafeGitLabURL("the GitLab URL must not contain a fragment")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeGitLabURL("the GitLab URL has no host")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise UnsafeGitLabURL(
            "the GitLab host must be ASCII (use its punycode form)") from exc
    if scheme == "http" and not http_allowed(host):
        raise UnsafeGitLabURL(
            f"the GitLab URL must use https:// — plain http to '{host}' needs the "
            "host in GITLAB_HTTP_ALLOWED_HOSTS (set by the server operator)"
        )
    path = parts.path.rstrip("/")
    while path.lower().endswith(API_SUFFIX):
        path = path[: -len(API_SUFFIX)].rstrip("/")
    segments = [s for s in path.split("/") if s]
    if path and not path.startswith("/"):
        raise UnsafeGitLabURL("the GitLab URL path is not valid")
    for seg in segments:
        if seg in (".", "..") or seg == "-" or not _SEGMENT.match(seg):
            raise UnsafeGitLabURL(
                "the GitLab URL must be the instance address (e.g. "
                "https://gitlab.example.com), not a project or page URL"
            )
    path = "/" + "/".join(segments) if segments else ""
    netloc = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}{path}"


# ─── addresses ───────────────────────────────────────────────────────


def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to. Patched in tests."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _v4_first(addresses: list[str]) -> list[str]:
    def is_v6(a: str) -> bool:
        try:
            return isinstance(ipaddress.ip_address(a.split("%", 1)[0]),
                              ipaddress.IPv6Address)
        except ValueError:
            return True
    return sorted(addresses, key=is_v6)


def check_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve ``host`` and refuse any address Celmis must not send a token to.

    Returns the addresses, IPv4 first. Raises :class:`UnsafeGitLabURL`.
    """
    from src.llm.litellm_proxy import address_is_blocked

    allowlisted = private_allowed(host)
    try:
        addresses = _resolve(host, port)
    except (OSError, UnicodeError) as exc:
        raise UnsafeGitLabURL(f"the GitLab host '{host}' does not resolve from the "
                              "Celmis server") from exc
    if not addresses:
        raise UnsafeGitLabURL(f"the GitLab host '{host}' does not resolve from the "
                              "Celmis server")
    blocked = [a for a in addresses if address_is_blocked(a, allowlisted=allowlisted)]
    if blocked:
        hint = ("" if allowlisted else
                " — a GitLab on a private network needs its host in "
                "GITLAB_ALLOWED_HOSTS (set by the server operator)")
        raise UnsafeGitLabURL(
            f"the GitLab host '{host}' resolves to a non-public address "
            f"({', '.join(blocked[:3])}){hint}"
        )
    return tuple(_v4_first(addresses))


# ─── the instance ────────────────────────────────────────────────────


@dataclass(frozen=True)
class GitLabInstance:
    """A normalised GitLab base URL and everything derived from it."""

    base_url: str = DEFAULT_BASE_URL

    @property
    def _parts(self):
        return urlsplit(self.base_url)

    @property
    def scheme(self) -> str:
        return self._parts.scheme

    @property
    def host(self) -> str:
        return (self._parts.hostname or "").lower()

    @property
    def port(self) -> int:
        return self._parts.port or (443 if self.scheme == "https" else 80)

    @property
    def netloc(self) -> str:
        return self._parts.netloc.lower()

    @property
    def path_prefix(self) -> str:
        return self._parts.path.rstrip("/")

    @property
    def api_base(self) -> str:
        return f"{self.base_url}{API_SUFFIX}"

    @property
    def is_default(self) -> bool:
        return self.base_url == DEFAULT_BASE_URL

    def web_url(self, full_path: str) -> str:
        return f"{self.base_url}/{full_path.strip('/')}"

    def clone_url(self, full_path: str) -> str:
        return f"{self.base_url}/{full_path.strip('/')}.git"

    def relative_path(self, url: str) -> str | None:
        """The part of ``url`` after this instance's base, or None if ``url``
        is not on this instance (other host, port, scheme or sub-path)."""
        try:
            parts = urlsplit(url.strip())
            port = parts.port
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or parts.username or parts.password:
            return None
        host = (parts.hostname or "").lower().rstrip(".")
        if host != self.host:
            return None
        if (port or (443 if scheme == "https" else 80)) != self.port:
            return None
        # Same scheme, except that an https link to an http-allowed instance
        # is the same instance; an http link to an https instance is not.
        if scheme != self.scheme and not (scheme == "https" and self.scheme == "http"):
            return None
        path = parts.path
        prefix = self.path_prefix
        if prefix:
            if not (path == prefix or path.startswith(prefix + "/")):
                return None
            path = path[len(prefix):]
        return path.strip("/")

    def owns_url(self, url: str) -> bool:
        return self.relative_path(url) is not None

    # ── network ──

    def addresses(self) -> tuple[str, ...]:
        """Resolve + check now. gitlab.com is not re-checked (shipped allowlist)."""
        if self.is_default:
            return ()
        return check_addresses(self.host, self.port)

    def http_kwargs(self) -> dict[str, Any]:
        """``build_client`` keywords that let a client reach THIS instance only.

        Empty for gitlab.com — it is on the shipped allowlist and nothing about
        the client changes. For a self-hosted instance: the host as the one
        extra allowed destination, the freshly validated address pinned, and
        the operator's CA bundle if one is configured. Raises
        :class:`UnsafeGitLabURL` when the host no longer passes the address
        rules.
        """
        if self.is_default:
            return {}
        addrs = self.addresses()
        kwargs: dict[str, Any] = {
            "extra_allowed_hosts": (self.host,),
            "pinned_addresses": {self.host: addrs[0]},
        }
        bundle = ca_bundle()
        if bundle:
            kwargs["ca_bundle"] = bundle
        return kwargs

    def git_config(self) -> list[tuple[str, str]]:
        """``git -c`` pairs for a clone/fetch from this instance.

        Pins the validated address (``http.curloptResolve``) and adds the
        operator CA bundle (``http.sslCAInfo``) — self-hosted only.
        """
        if self.is_default:
            return []
        addrs = self.addresses()
        addr = addrs[0]
        shown = f"[{addr}]" if ":" in addr else addr
        pairs = [("http.curloptResolve", f"{self.host}:{self.port}:{shown}")]
        bundle = ca_bundle()
        if bundle:
            pairs.append(("http.sslCAInfo", bundle))
        return pairs


DEFAULT_INSTANCE: Final[GitLabInstance] = GitLabInstance()


def instance_of(base_url: object) -> GitLabInstance:
    """A :class:`GitLabInstance` for a stored/normalised value. No DNS."""
    if base_url in (None, ""):
        return DEFAULT_INSTANCE
    return GitLabInstance(normalise_base_url(base_url))


def validate_base_url(raw: object) -> GitLabInstance:
    """Normalise AND resolve — what a save does before it writes anything."""
    instance = instance_of(raw)
    instance.addresses()
    return instance


def instance_from_metadata(metadata: Mapping[str, Any] | None) -> GitLabInstance:
    """The instance a stored GitLab credential belongs to.

    A row without the key is a gitlab.com row (every row saved before
    self-hosted support). A row whose stored value no longer normalises —
    hand-edited, or an http:// host removed from the allowlist — RAISES
    rather than falling back to gitlab.com: that fallback would send a
    self-hosted token to gitlab.com.
    """
    raw = (metadata or {}).get(METADATA_KEY) if isinstance(metadata, Mapping) else None
    return instance_of(raw)


def instance_for_credential(credential: Any) -> GitLabInstance:
    return instance_from_metadata(getattr(credential, "metadata", None))


def gitlab_kwarg(provider: str, credential: Any) -> dict[str, GitLabInstance]:
    """``{"gitlab": instance}`` for a GitLab credential, ``{}`` otherwise —
    for the listing helpers that take an optional ``gitlab=`` keyword."""
    if str(provider).lower() != "gitlab" or credential is None:
        return {}
    return {"gitlab": instance_for_credential(credential)}


def instance_for_workspace(workspace_id: str, *, user_id: str = "default",
                           store=None) -> GitLabInstance:
    """The workspace's configured instance; gitlab.com when it has no GitLab
    connection (nothing to authenticate with anyway)."""
    from src.credentials import resolve_git_credential

    cred = resolve_git_credential("gitlab", user_id=user_id,
                                  workspace_id=workspace_id, store=store)
    if cred is None:
        return DEFAULT_INSTANCE
    return instance_for_credential(cred)


def build_gitlab_client(instance: GitLabInstance, *, token: str | None = None,
                        timeout: Any = 30.0, headers: Mapping[str, str] | None = None,
                        follow_redirects: bool = False):
    """A guarded httpx client that can reach ``instance`` (and the shipped
    allowlist) — nothing else. ``PRIVATE-TOKEN`` set when ``token`` is given."""
    from src.http import build_client

    merged: dict[str, str] = {"Accept": "application/json"}
    merged.update(headers or {})
    if token:
        merged["PRIVATE-TOKEN"] = token
    return build_client(timeout=timeout, headers=merged,
                        follow_redirects=follow_redirects, **instance.http_kwargs())


__all__ = [
    "API_SUFFIX",
    "DEFAULT_BASE_URL",
    "DEFAULT_HOST",
    "DEFAULT_INSTANCE",
    "METADATA_KEY",
    "GitLabInstance",
    "UnsafeGitLabURL",
    "build_gitlab_client",
    "check_addresses",
    "instance_for_credential",
    "instance_for_workspace",
    "instance_from_metadata",
    "instance_of",
    "normalise_base_url",
    "validate_base_url",
]
