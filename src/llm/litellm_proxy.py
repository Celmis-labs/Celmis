"""A workspace's OWN LiteLLM proxy — base URL + virtual key, per tenant.

Not to be confused with :mod:`src.llm.gateway`. The gateway is the
installation's proxy: Celmis holds its master key and mints one virtual key
per workspace from that workspace's real provider keys. This module is the
other direction — the customer already runs a LiteLLM proxy (their company's
"one door to every model"), hands us a virtual key ``sk-…`` and the proxy's
address, and every call for a surface set to provider ``litellm`` goes there.

Before this existed, the natural thing to do with such a key was to paste it
into the OpenAI field, where the Test button sent it to api.openai.com and
OpenAI answered 401 for a key it never issued.

Storage: ONE credentials row, provider ``litellm``, slot ``ws:{id}``. The key
is the secret, the address is ``metadata.base_url``. One row on purpose — the
two are only meaningful as a pair, and two rows can be changed one at a time:

  * a stored key paired with someone else's URL is how a workspace admin who
    never saw the key exfiltrates it (re-point the URL at a host they control,
    press Test). So changing the URL requires re-entering the key — see
    ``put_config`` and ``test_connection``.
  * the env fallback is the same pair: ``LITELLM_API_KEY`` is only ever sent
    to ``LITELLM_API_BASE``. A tenant-chosen URL never receives the operator's
    env key, and a tenant's stored key never goes to the env URL.

Calls go out as ``litellm_proxy/<model>`` with an explicit ``api_base`` — the
installed LiteLLM's ``litellm_proxy`` route posts the bare model name to the
proxy's OpenAI-compatible endpoints. Without an explicit ``api_base`` that
route falls back to ``LITELLM_PROXY_API_BASE``, which on a gateway install is
the INSTALLATION'S proxy — so every caller refuses instead (fail-closed, like
the self-hosted ``openai_compatible`` profiles).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Provider slug used everywhere (profiles, credentials rows, the UI).
PROVIDER: Final[str] = "litellm"
#: Env fallback — the operator's house proxy. Both or neither; see module doc.
ENV_KEY: Final[str] = "LITELLM_API_KEY"
ENV_BASE: Final[str] = "LITELLM_API_BASE"

_LABEL: Final[str] = "default"


class LiteLLMProxyError(RuntimeError):
    """The workspace's LiteLLM proxy is not configured or cannot be used."""


@dataclass(frozen=True)
class Endpoint:
    base_url: str           # as stored: no trailing slash, "/v1" optional
    api_key: str
    source: str             # "ui" (credentials row) | "env"


# ─── URL helpers ─────────────────────────────────────────────────────


def normalise_base_url(raw: object) -> str:
    """http(s), non-empty, no trailing slash. Raises ValueError otherwise.

    "/v1" at the end is accepted and kept: the proxy serves its OpenAI routes
    both with and without it, and people copy whichever their docs showed.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("LiteLLM base URL must be a non-empty string")
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError(
            "LiteLLM base URL must start with http:// or https:// "
            "(e.g. https://litellm.example.com)"
        )
    if not urlsplit(url).hostname:
        raise ValueError("LiteLLM base URL has no host")
    return url


def _root(base_url: str) -> str:
    """The proxy root — ``base_url`` minus a trailing ``/v1``."""
    return base_url[:-3] if base_url.endswith("/v1") else base_url


def models_url(base_url: str) -> str:
    """OpenAI-compatible model list: ``{root}/v1/models``."""
    return f"{_root(base_url)}/v1/models"


def embeddings_url(base_url: str) -> str:
    """OpenAI-compatible embeddings: ``{root}/v1/embeddings``."""
    return f"{_root(base_url)}/v1/embeddings"


def model_info_url(base_url: str) -> str:
    """LiteLLM's own ``/model/info`` (alias → litellm_params.model, mode)."""
    return f"{_root(base_url)}/model/info"


def host_of(base_url: str) -> str:
    return (urlsplit(base_url).hostname or "").lower()


# ─── Storage ─────────────────────────────────────────────────────────


def is_usable_key(value: str) -> bool:
    from src.llm.keys import _is_placeholder

    return bool(value) and not _is_placeholder(value)


def stored_endpoint(workspace_id: str) -> Endpoint | None:
    """The ``ws:{id}`` row only — no legacy slots, no env. For the settings
    page ("what did THIS workspace save") and the re-enter-the-key rule."""
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError
    from src.llm.keys import workspace_slot

    try:
        row = get_credential_store().load(
            provider=PROVIDER, user_id=workspace_slot(workspace_id),
            account_label=_LABEL,
        )
    except CredentialStoreError:
        return None
    except Exception as exc:  # noqa: BLE001 — no store → nothing stored
        logger.debug("litellm_proxy_store_unavailable err=%s", exc)
        return None
    if row is None or not is_usable_key(row.secret or ""):
        return None
    base = str((row.metadata or {}).get("base_url") or "").strip()
    if not base:
        return None
    return Endpoint(base_url=base, api_key=row.secret, source="ui")


def env_endpoint() -> Endpoint | None:
    key = os.environ.get(ENV_KEY, "").strip()
    raw = os.environ.get(ENV_BASE, "").strip()
    if not (is_usable_key(key) and raw):
        return None
    try:
        base = normalise_base_url(raw)
    except ValueError:
        logger.warning("litellm_env_base_invalid var=%s", ENV_BASE)
        return None
    return Endpoint(base_url=base, api_key=key, source="env")


def resolve_endpoint(workspace_id: str = "default", user_id: str = "default") -> Endpoint | None:
    """The (base URL, key) pair this workspace calls its proxy with, or None.

    Same slot chain as every other provider key (:func:`src.llm.keys._slot_chain`
    — a non-default workspace reads ONLY its own ``ws:{id}`` row), then the env
    pair. A row is used only when it carries BOTH halves; a half is never
    completed from another source.
    """
    from src.credentials.store import CredentialStoreError
    from src.llm.keys import _slot_chain

    try:
        from src.credentials import get_credential_store

        store = get_credential_store()
    except Exception as exc:  # noqa: BLE001
        logger.debug("litellm_proxy_store_unavailable err=%s", exc)
        store = None
    if store is not None:
        for slot in _slot_chain(workspace_id, user_id):
            try:
                row = store.load(provider=PROVIDER, user_id=slot, account_label=_LABEL)
            except CredentialStoreError:
                continue
            if row is None or not is_usable_key(row.secret or ""):
                continue
            base = str((row.metadata or {}).get("base_url") or "").strip()
            if not base:
                logger.warning(
                    "litellm_proxy_row_without_base workspace=%s slot=%s — "
                    "ignored; re-save the LiteLLM proxy on /settings/llm",
                    workspace_id, slot,
                )
                continue
            return Endpoint(base_url=base, api_key=row.secret, source="ui")
    return env_endpoint()


def save_endpoint(workspace_id: str, *, base_url: str, api_key: str) -> None:
    """Write the pair to ``ws:{id}`` — always both halves together."""
    from src.credentials import get_credential_store
    from src.llm.keys import workspace_slot

    base = normalise_base_url(base_url)
    get_credential_store().save(
        provider=PROVIDER, secret=api_key,
        metadata={"saved_via": "llm_profiles", "base_url": base},
        user_id=workspace_slot(workspace_id), account_label=_LABEL,
    )
    reset_cache()


def delete_endpoint(workspace_id: str) -> bool:
    """Remove this workspace's ``ws:{id}`` row. True if there was one.

    A revoked or leaked virtual key must be removable, not only overwritable.
    The env pair (if any) is untouched — it is the operator's, not the
    workspace's.
    """
    from src.credentials import get_credential_store
    from src.llm.keys import workspace_slot

    store = get_credential_store()
    deleted = bool(store.delete(
        provider=PROVIDER, user_id=workspace_slot(workspace_id), account_label=_LABEL,
    ))
    reset_cache()
    return deleted


# ─── HTTP ────────────────────────────────────────────────────────────


def _is_public_host(host: str) -> bool:
    """True only when EVERY address `host` resolves to is globally routable.

    Why not just add the host to the allowlist like the vendor pings do: an
    exact allowlist match short-circuits the private-network rule in
    :func:`src.security.egress.host_is_allowed`, and this host is typed by a
    workspace admin — "http://169.254.169.254" would become reachable. So the
    host extends the allowlist only when it is public; a LAN/compose proxy is
    then reachable exactly when the operator set
    ``EGRESS_ALLOW_PRIVATE_NETWORK=1`` (link-local stays refused even then),
    the same policy the self-hosted probes follow.

    Residual risk, stated: a name that resolves public here and private a
    moment later (DNS rebinding) is not caught by a check-then-connect. The
    transport re-checks only the allowlist, not the address.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def check_base_url_egress(base_url: str) -> None:
    """Refuse a proxy address the egress policy would not let us reach.

    Real chat, review and embeddings calls go through the litellm SDK's own
    HTTP client, which no allowlist transport wraps. So the rule is applied
    when the address is SAVED: a public host is fine (it is what
    :func:`ping_client` allowlists too); a private, loopback or unresolvable
    one only when the operator opted in with ``EGRESS_ALLOW_PRIVATE_NETWORK=1``
    (link-local, i.e. 169.254.169.254, stays refused even then) or listed it
    in ``EGRESS_ALLOWED_HOSTS``. Same policy :func:`src.http.build_client`
    applies to Test.

    Residual risk, stated: this is a check at save time. A name that later
    re-resolves somewhere else (DNS rebinding) is not re-checked per call.
    """
    host = host_of(base_url)
    if _is_public_host(host):
        return
    from src.config import get_settings
    from src.http import allowed_hosts
    from src.security.egress import host_is_allowed

    if host_is_allowed(
        host, allowed_hosts(()),
        allow_private_network=bool(get_settings().egress_allow_private_network),
    ):
        return
    raise LiteLLMProxyError(
        f"the LiteLLM proxy host '{host}' is not a public address (or does not "
        "resolve) and the egress policy does not allow it — a proxy on a "
        "private network needs EGRESS_ALLOW_PRIVATE_NETWORK=1 or its host in "
        "EGRESS_ALLOWED_HOSTS"
    )


def ping_client(base_url: str, *, timeout: float):
    """Guarded httpx client for calls to this proxy (see :func:`_is_public_host`)."""
    from src.http import build_client

    host = host_of(base_url)
    extra = (host,) if _is_public_host(host) else ()
    return build_client(timeout=timeout, extra_allowed_hosts=extra)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def model_ids(body: Any) -> list[str]:
    """Model ids from an OpenAI-shaped ``{data: [{id}]}`` body, de-duplicated."""
    data = (body.get("data") or body.get("models") or []) if isinstance(body, dict) else []
    ids: list[str] = []
    for m in data:
        mid = m.get("id") if isinstance(m, dict) else (m if isinstance(m, str) else None)
        if mid and str(mid) not in ids:
            ids.append(str(mid))
    return ids


def fetch_model_info(ep: Endpoint, *, timeout: float = 8.0) -> dict[str, dict]:
    """``GET /model/info`` → {alias: {"underlying": str|None, "mode": str|None}}.

    Best effort: a virtual key may be refused this route (it is an admin-ish
    endpoint on some proxy versions), and then the answer is ``{}`` — callers
    degrade to "unknown", never fail.
    """
    try:
        with ping_client(ep.base_url, timeout=timeout) as client:
            resp = client.get(model_info_url(ep.base_url), headers=_auth(ep.api_key))
        if resp.status_code != 200:
            return {}
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("litellm_model_info_failed host=%s err=%s", host_of(ep.base_url), exc)
        return {}
    out: dict[str, dict] = {}
    for item in (body.get("data") or []) if isinstance(body, dict) else []:
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
# An EMPTY answer (refused route, network blip, 5xx) is remembered only
# briefly: long enough not to hammer a proxy that refuses /model/info on every
# completion, short enough that one blip does not cost an hour of pricing.
_MODEL_INFO_EMPTY_TTL = 60.0
_MODEL_INFO_CACHE: dict[tuple[str, str], tuple[float, dict[str, dict]]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(ep: Endpoint) -> tuple[str, str]:
    import hashlib

    return ep.base_url, hashlib.sha256(ep.api_key.encode()).hexdigest()[:16]


def cached_model_info(ep: Endpoint) -> dict[str, dict]:
    key = _cache_key(ep)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _MODEL_INFO_CACHE.get(key)
        ttl = _MODEL_INFO_TTL if (hit is not None and hit[1]) else _MODEL_INFO_EMPTY_TTL
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    info = fetch_model_info(ep, timeout=5.0)
    with _CACHE_LOCK:
        _MODEL_INFO_CACHE[key] = (now, info)
    return info


def underlying_model(ep: Endpoint | None, alias: str) -> str | None:
    """The model a proxy alias runs on (``litellm_params.model``), or None.

    Used for pricing only: ``litellm_proxy/<alias>`` is in no price table, the
    underlying ``gemini/gemini-3-flash`` usually is.
    """
    if ep is None or not alias:
        return None
    return (cached_model_info(ep).get(alias) or {}).get("underlying") or None


def reset_cache() -> None:
    with _CACHE_LOCK:
        _MODEL_INFO_CACHE.clear()


def require_api_base(api_base: str | None) -> str:
    """Fail-closed guard every call site uses before handing litellm a model
    string ``litellm_proxy/…``: no address → refuse, never let LiteLLM read
    ``LITELLM_PROXY_API_BASE`` (the installation's gateway) instead."""
    if not api_base:
        raise LiteLLMProxyError(
            "LiteLLM proxy profile has no base URL — set the LiteLLM proxy "
            "(base URL + key) in /settings/llm; refusing to fall back to the "
            "installation's proxy"
        )
    return api_base


__all__ = [
    "PROVIDER",
    "ENV_KEY",
    "ENV_BASE",
    "Endpoint",
    "LiteLLMProxyError",
    "normalise_base_url",
    "models_url",
    "model_info_url",
    "stored_endpoint",
    "env_endpoint",
    "resolve_endpoint",
    "save_endpoint",
    "delete_endpoint",
    "check_base_url_egress",
    "embeddings_url",
    "ping_client",
    "model_ids",
    "fetch_model_info",
    "cached_model_info",
    "underlying_model",
    "reset_cache",
    "is_usable_key",
    "require_api_base",
]
