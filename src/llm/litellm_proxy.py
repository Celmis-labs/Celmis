"""A workspace's OWN LiteLLM proxy — base URL + virtual key, per tenant.

Not to be confused with :mod:`src.llm.gateway`. The gateway is the
installation's proxy: Celmis holds its master key and mints one virtual key
per workspace (LITELLM_PROXY_URL + LITELLM_MASTER_KEY). This module is the
other direction — the customer already runs a LiteLLM proxy, hands us a
virtual key ``sk-…`` and the proxy's address, and every call for a surface set
to provider ``litellm`` goes there.

Configured ONLY through the UI (Settings → LLM), by a workspace admin. There
is deliberately no env fallback: an operator-wide key would be a credential
every tenant's profile could route through.

Storage
-------
ONE credentials row, provider ``litellm``, slot ``ws:{id}``. The Fernet-
encrypted secret is the JSON ``{"url": ..., "key": ...}`` — the address is
encrypted too, never kept in plaintext metadata (it is often an internal
hostname). Metadata carries only ``fingerprint`` = first 12 hex of
sha256(key), for display and audit. Hashing the key instead of encrypting it
is impossible: it has to be sent.

Changing the URL always means typing the key again (the save endpoint takes
both or nothing): a stored key paired with a new URL is how an admin who never
saw the key would exfiltrate it to a host they control.

URL safety (:func:`validate_target`)
------------------------------------
One function, used at save time, on Test, on every model listing and — with a
short cache — before every real completion/embedding call:

  * https only; no userinfo, query or fragment; trailing ``/`` and ``/v1``
    are normalised away (the stored base is the proxy ROOT);
  * the hostname is resolved and EVERY address must be globally routable:
    private, loopback, link-local (169.254.169.254), multicast, reserved,
    unspecified, CGNAT 100.64/10, IPv6 ULA/link-local/site-local and IPv4
    embedded in IPv6 (mapped, 6to4, Teredo, NAT64) are all refused;
  * operator escape hatch for a LAN proxy: ``LITELLM_PROXY_ALLOWED_HOSTS``
    (JSON list, same exact-or-subdomain semantics as EGRESS_ALLOWED_HOSTS).
    A listed host may resolve to private/loopback/CGNAT/ULA addresses — never
    link-local, multicast or unspecified. Strict by default (empty list).
    ``EGRESS_ALLOW_PRIVATE_NETWORK`` does NOT open this path.

Requests Celmis itself makes (/v1/models, /model/info, the embeddings probe)
are PINNED to the validated address: the guarded transport connects to that
IP while TLS SNI and the Host header stay the hostname (certificate checked
against the name). No redirects, connect/read timeouts, 2 MB response cap.

Residual risk, stated: real chat/review/embeddings calls go through the
litellm SDK's own HTTP stack, which cannot be pinned from here. Before each
such call :func:`require_api_base` re-resolves the host and re-applies the
same address rules (cached for ``_CALL_CHECK_TTL`` seconds). A DNS-rebinding
attacker who answers public to us and private to the SDK's lookup a few
milliseconds later is not excluded on that path; the key would then go to an
internal address over TLS, where the certificate must still match the
hostname — so the practical exposure needs an internal host holding a valid
certificate for the attacker's name.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Provider slug used everywhere (profiles, credentials rows, the UI).
PROVIDER: Final[str] = "litellm"

_LABEL: Final[str] = "default"
#: Response size cap for anything read from the proxy (bytes).
MAX_RESPONSE_BYTES: Final[int] = 2 * 1024 * 1024
#: How long a call-time re-validation of the host stays good (seconds).
_CALL_CHECK_TTL: Final[float] = 30.0

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class LiteLLMProxyError(RuntimeError):
    """The workspace's LiteLLM proxy is not configured or cannot be used.

    Messages never carry the key or the full URL — they may reach a log line
    or an HTTP response.
    """


class UnsafeProxyURL(LiteLLMProxyError):
    """The address fails the URL safety rules (see module doc)."""


class ProxyAuthError(LiteLLMProxyError):
    """The proxy answered 401/403 — the virtual key was rejected."""


@dataclass(frozen=True)
class Endpoint:
    base_url: str           # normalised proxy root (https, no trailing /, no /v1)
    api_key: str
    source: str = "ui"      # "ui" (credentials row) | "profile"

    @property
    def host(self) -> str:
        return host_of(self.base_url)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.api_key)


@dataclass(frozen=True)
class Target:
    """A base URL that passed :func:`validate_target`."""

    base_url: str
    host: str
    port: int
    addresses: tuple[str, ...]


def fingerprint(api_key: str) -> str:
    """First 12 hex of sha256(key): identifies a key without revealing it."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def mask_key(api_key: str) -> str:
    """``…abcd`` — the last 4 characters, nothing else."""
    return f"…{api_key[-4:]}" if len(api_key) >= 8 else "…"


# ─── URL rules ───────────────────────────────────────────────────────


def normalise_base_url(raw: object) -> str:
    """https, host, no userinfo/query/fragment; strip trailing ``/`` and ``/v1``.

    Raises :class:`UnsafeProxyURL`. The result is the proxy ROOT, e.g.
    ``https://litellm.example.com`` or ``https://gw.example.com/litellm``.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafeProxyURL("the LiteLLM proxy URL is empty")
    text = raw.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise UnsafeProxyURL("the LiteLLM proxy URL is not a valid URL") from exc
    if parts.scheme.lower() != "https":
        raise UnsafeProxyURL("the LiteLLM proxy URL must use https://")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise UnsafeProxyURL("the LiteLLM proxy URL must not contain user:password@")
    if parts.query or text.rstrip("/").endswith("?"):
        raise UnsafeProxyURL("the LiteLLM proxy URL must not contain a query string")
    if parts.fragment or "#" in text:
        raise UnsafeProxyURL("the LiteLLM proxy URL must not contain a fragment")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeProxyURL("the LiteLLM proxy URL has no host")
    path = parts.path.rstrip("/")
    while path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    if any(c.isspace() for c in path) or "\\" in path:
        raise UnsafeProxyURL("the LiteLLM proxy URL path is not valid")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return f"https://{netloc}{path}"


def host_of(base_url: str) -> str:
    return (urlsplit(base_url).hostname or "").lower()


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if int(ip) >> 32 == 0 and int(ip) > 1:       # deprecated ::a.b.c.d
        return ipaddress.IPv4Address(int(ip))
    return None


def _never_ok(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Refused even for an operator-allowlisted host."""
    return ip.is_link_local or ip.is_multicast or ip.is_unspecified


def _not_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (_never_ok(ip) or ip.is_private or ip.is_loopback or ip.is_reserved
            or not ip.is_global):
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return True
    return isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local


def address_is_blocked(address: str, *, allowlisted: bool = False) -> bool:
    """True when Celmis must not connect to ``address`` for a proxy call."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        v4 = _embedded_v4(ip)
        if v4 is not None:
            candidates.append(v4)
    check = _never_ok if allowlisted else _not_public
    return any(check(c) for c in candidates)


def _allowlisted(host: str) -> bool:
    """``LITELLM_PROXY_ALLOWED_HOSTS`` — the operator's LAN escape hatch."""
    from src.config import get_settings
    from src.security.egress import host_is_allowed

    hosts = list(getattr(get_settings(), "litellm_proxy_allowed_hosts", None) or [])
    return bool(hosts) and host_is_allowed(host, hosts, allow_private_network=False)


def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to. Patched in tests."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def validate_target(raw_url: object) -> Target:
    """THE URL check: normalise, resolve, refuse any non-public address.

    Raises :class:`UnsafeProxyURL`. See the module doc for the rules.
    """
    base = normalise_base_url(raw_url)
    parts = urlsplit(base)
    host = (parts.hostname or "").lower()
    port = parts.port or 443
    allowlisted = _allowlisted(host)
    try:
        addresses = _resolve(host, port)
    except (OSError, UnicodeError) as exc:
        raise UnsafeProxyURL(
            f"the LiteLLM proxy host '{host}' does not resolve"
        ) from exc
    if not addresses:
        raise UnsafeProxyURL(f"the LiteLLM proxy host '{host}' does not resolve")
    blocked = [a for a in addresses if address_is_blocked(a, allowlisted=allowlisted)]
    if blocked:
        hint = ("" if allowlisted else
                " — a proxy on a private network needs its host in "
                "LITELLM_PROXY_ALLOWED_HOSTS (set by the server operator)")
        raise UnsafeProxyURL(
            f"the LiteLLM proxy host '{host}' resolves to a non-public address "
            f"({', '.join(blocked[:3])}){hint}"
        )
    return Target(base_url=base, host=host, port=port, addresses=tuple(addresses))


# ─── Pinned HTTP ─────────────────────────────────────────────────────


def _timeout():
    import httpx

    return httpx.Timeout(15.0, connect=5.0)


def request_json(
    target: Target, method: str, path: str, api_key: str, *,
    json_body: dict | None = None, max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[int, Any]:
    """One request to the proxy, pinned to the validated address.

    Returns ``(status, parsed JSON or None)``. Raises :class:`LiteLLMProxyError`
    on network failure, a redirect, or a body over ``max_bytes``. Never puts
    the key or the URL into an exception message.
    """
    import httpx

    from src.http import build_client
    from src.security.egress import EgressBlockedError

    url = f"{target.base_url}{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        with build_client(
            timeout=_timeout(), follow_redirects=False,
            extra_allowed_hosts=(target.host,),
            pinned_addresses={target.host: target.addresses[0]},
        ) as client, client.stream(method, url, headers=headers, json=json_body) as resp:
            status = resp.status_code
            if 300 <= status < 400:
                raise LiteLLMProxyError(
                    f"the LiteLLM proxy answered a redirect ({status}) for {path}; "
                    "redirects are not followed — use the final URL"
                )
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise LiteLLMProxyError(
                    f"the LiteLLM proxy response for {path} is too large"
                )
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise LiteLLMProxyError(
                        f"the LiteLLM proxy response for {path} is too large"
                    )
    except EgressBlockedError as exc:
        raise LiteLLMProxyError("egress to the LiteLLM proxy was blocked") from exc
    except httpx.HTTPError as exc:
        raise LiteLLMProxyError(
            f"could not reach the LiteLLM proxy ({type(exc).__name__})"
        ) from exc
    try:
        body = json.loads(bytes(buf)) if buf else None
    except ValueError:
        body = None
    return status, body


def model_ids(body: Any) -> list[str]:
    """Model ids from an OpenAI-shaped ``{data: [{id}]}`` body, de-duplicated."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for m in data:
        mid = m.get("id") if isinstance(m, dict) else None
        if isinstance(mid, str) and mid.strip() and mid not in ids and len(mid) <= 300:
            ids.append(mid)
    return ids


def list_models(base_url: str, api_key: str) -> tuple[Target, list[str]]:
    """Validate ``base_url`` and GET ``/v1/models`` with the key.

    Raises :class:`UnsafeProxyURL`, :class:`ProxyAuthError` or
    :class:`LiteLLMProxyError`. Returns the target and a NON-empty id list.
    """
    target = validate_target(base_url)
    status, body = request_json(target, "GET", "/v1/models", api_key)
    if status in (401, 403):
        raise ProxyAuthError(f"the LiteLLM proxy rejected the virtual key ({status})")
    if status != 200:
        raise LiteLLMProxyError(f"the LiteLLM proxy returned {status} for /v1/models")
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise LiteLLMProxyError(
            "the LiteLLM proxy did not return a model list for /v1/models"
        )
    ids = model_ids(body)
    if not ids:
        raise LiteLLMProxyError("the LiteLLM proxy lists no models for this key")
    return target, ids


def validate_proxy(base_url: object, api_key: object) -> tuple[Endpoint, list[str]]:
    """Every check a save needs, in order; nothing is written here.

    Key shape → URL rules → DNS/address rules → GET /v1/models = 200 with a
    non-empty list. Returns the normalised endpoint and the model ids.
    """
    key = api_key.strip() if isinstance(api_key, str) else ""
    if not is_usable_key(key):
        raise LiteLLMProxyError(
            "the LiteLLM virtual key is missing or does not look like a key"
        )
    target, ids = list_models(str(base_url or ""), key)
    return Endpoint(base_url=target.base_url, api_key=key), ids


# ─── Storage ─────────────────────────────────────────────────────────


def is_usable_key(value: str) -> bool:
    from src.llm.keys import _is_placeholder

    return bool(value) and not _is_placeholder(value) and len(value) <= 500 \
        and not any(c.isspace() for c in value)


def _decode(secret: str) -> Endpoint | None:
    try:
        data = json.loads(secret)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    url, key = data.get("url"), data.get("key")
    if not (isinstance(url, str) and isinstance(key, str) and is_usable_key(key)):
        return None
    return Endpoint(base_url=url, api_key=key, source="ui")


def _load(slot: str) -> Endpoint | None:
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError

    try:
        row = get_credential_store().load(
            provider=PROVIDER, user_id=slot, account_label=_LABEL,
        )
    except CredentialStoreError:
        logger.warning("litellm_proxy_row_unreadable slot=%s", slot)
        return None
    except Exception as exc:  # noqa: BLE001 — no store → nothing stored
        logger.debug("litellm_proxy_store_unavailable err=%s", type(exc).__name__)
        return None
    if row is None or not row.secret:
        return None
    ep = _decode(row.secret)
    if ep is None:
        logger.warning(
            "litellm_proxy_row_invalid slot=%s — re-save the LiteLLM proxy on "
            "/settings/llm", slot,
        )
    return ep


def stored_endpoint(workspace_id: str) -> Endpoint | None:
    """This workspace's own ``ws:{id}`` row only."""
    from src.llm.keys import workspace_slot

    return _load(workspace_slot(workspace_id))


def resolve_endpoint(workspace_id: str = "default", user_id: str = "default") -> Endpoint | None:
    """The (base URL, key) pair this workspace calls its proxy with, or None.

    Same slot chain as every other provider key (a non-default workspace reads
    ONLY its own ``ws:{id}`` row). No env fallback — UI only.
    """
    from src.llm.keys import _slot_chain

    for slot in _slot_chain(workspace_id, user_id):
        ep = _load(slot)
        if ep is not None:
            return ep
    return None


def save_endpoint(workspace_id: str, endpoint: Endpoint) -> None:
    """Write a VALIDATED endpoint (from :func:`validate_proxy`) to ``ws:{id}``.

    URL and key are encrypted together as the secret; metadata holds only the
    fingerprint.
    """
    from src.credentials import get_credential_store
    from src.llm.keys import workspace_slot

    secret = json.dumps({"url": endpoint.base_url, "key": endpoint.api_key})
    get_credential_store().save(
        provider=PROVIDER, secret=secret,
        metadata={"saved_via": "llm_settings", "fingerprint": endpoint.fingerprint},
        user_id=workspace_slot(workspace_id), account_label=_LABEL,
    )
    reset_cache()


def delete_endpoint(workspace_id: str) -> bool:
    """Remove this workspace's ``ws:{id}`` row. True if there was one."""
    from src.credentials import get_credential_store
    from src.llm.keys import workspace_slot

    deleted = bool(get_credential_store().delete(
        provider=PROVIDER, user_id=workspace_slot(workspace_id), account_label=_LABEL,
    ))
    reset_cache()
    return deleted


# ─── /model/info (pricing, mode split) ───────────────────────────────


def fetch_model_info(ep: Endpoint) -> dict[str, dict]:
    """``GET /model/info`` → {alias: {"underlying": str|None, "mode": str|None}}.

    Best effort: a virtual key may be refused this route, and then the answer
    is ``{}`` — callers degrade to "unknown", never fail.
    """
    try:
        target = validate_target(ep.base_url)
        status, body = request_json(target, "GET", "/model/info", ep.api_key)
    except LiteLLMProxyError as exc:
        logger.debug("litellm_model_info_failed err=%s", type(exc).__name__)
        return {}
    if status != 200 or not isinstance(body, dict):
        return {}
    out: dict[str, dict] = {}
    for item in body.get("data") or []:
        if not isinstance(item, dict) or not item.get("model_name"):
            continue
        params = item.get("litellm_params") or {}
        info = item.get("model_info") or {}
        out[str(item["model_name"])] = {
            "underlying": (params.get("model") if isinstance(params, dict) else None) or None,
            "mode": (info.get("mode") if isinstance(info, dict) else None) or None,
        }
    return out


# alias → underlying model, per (base, key-hash). Negative answers cached too:
# the spend path asks on every call and must not turn into one HTTP round trip
# per completion against a proxy that refuses /model/info.
_MODEL_INFO_TTL = 3600.0
_MODEL_INFO_EMPTY_TTL = 60.0
_MODEL_INFO_CACHE: dict[tuple[str, str], tuple[float, dict[str, dict]]] = {}
_CALL_CHECKS: dict[str, float] = {}
_CACHE_LOCK = threading.Lock()


def cached_model_info(ep: Endpoint) -> dict[str, dict]:
    key = (ep.base_url, ep.fingerprint)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _MODEL_INFO_CACHE.get(key)
        ttl = _MODEL_INFO_TTL if (hit is not None and hit[1]) else _MODEL_INFO_EMPTY_TTL
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    info = fetch_model_info(ep)
    with _CACHE_LOCK:
        _MODEL_INFO_CACHE[key] = (now, info)
    return info


def underlying_model(ep: Endpoint | None, alias: str) -> str | None:
    """The model a proxy alias runs on (``litellm_params.model``), or None."""
    if ep is None or not alias:
        return None
    return (cached_model_info(ep).get(alias) or {}).get("underlying") or None


def reset_cache() -> None:
    with _CACHE_LOCK:
        _MODEL_INFO_CACHE.clear()
        _CALL_CHECKS.clear()


# ─── Call-time guard ─────────────────────────────────────────────────


def ensure_call_target(api_base: str) -> str:
    """Re-validate the stored base before an SDK call (cached briefly).

    The SDK's own HTTP stack cannot be pinned, so the host is re-resolved and
    the address rules re-applied at most every ``_CALL_CHECK_TTL`` seconds.
    Raises :class:`UnsafeProxyURL`.
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        ok_at = _CALL_CHECKS.get(api_base)
    if ok_at is not None and now - ok_at < _CALL_CHECK_TTL:
        return api_base
    target = validate_target(api_base)
    if target.base_url != api_base:
        raise UnsafeProxyURL("the LiteLLM proxy base is not in normalised form")
    with _CACHE_LOCK:
        _CALL_CHECKS[api_base] = now
    return api_base


def require_api_base(api_base: str | None) -> str:
    """Fail-closed guard every call site uses before handing litellm a model
    string ``litellm_proxy/…``: no address → refuse (never let LiteLLM read
    ``LITELLM_PROXY_API_BASE``, the installation's gateway); an address that
    no longer passes the URL rules → refuse."""
    if not api_base:
        raise LiteLLMProxyError(
            "LiteLLM proxy profile has no base URL — set the LiteLLM proxy "
            "(URL + key) in Settings → LLM; refusing to fall back to the "
            "installation's proxy"
        )
    return ensure_call_target(api_base)


__all__ = [
    "PROVIDER",
    "MAX_RESPONSE_BYTES",
    "Endpoint",
    "Target",
    "LiteLLMProxyError",
    "UnsafeProxyURL",
    "ProxyAuthError",
    "fingerprint",
    "mask_key",
    "normalise_base_url",
    "address_is_blocked",
    "validate_target",
    "request_json",
    "model_ids",
    "list_models",
    "validate_proxy",
    "stored_endpoint",
    "resolve_endpoint",
    "save_endpoint",
    "delete_endpoint",
    "fetch_model_info",
    "cached_model_info",
    "underlying_model",
    "reset_cache",
    "is_usable_key",
    "ensure_call_target",
    "require_api_base",
]
