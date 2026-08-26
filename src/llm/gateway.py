"""LiteLLM gateway — one exit door to every LLM provider (Stage 24).

Off by default. The whole module is a no-op unless BOTH ``LITELLM_PROXY_URL``
and ``LITELLM_MASTER_KEY`` are set; :func:`is_enabled` is the single switch and
every caller checks it first, so a box without those env vars behaves exactly
as it did before this file existed.

What it buys us
---------------
Provider keys stop being handed to the application process on every call.
Instead each workspace gets, on the proxy:

    team        ``ws-{workspace}``            — grouping + the proxy's own spend
    deployments ``celmis-{ws}-chat|-review|-embed``
                                              — one per surface, holding the
                                                tenant's REAL provider key
    virtual key ``sk-…``                      — restricted to exactly those
                                                deployment names

The virtual key is what our process keeps (cached in the credential store), and
it can only reach that one tenant's deployments. Team-scoped model access is an
Enterprise feature, so the isolation is enforced by the key's ``models`` list —
which is why an EMPTY list is refused outright: on LiteLLM an empty ``models``
means *every* model, i.e. every other tenant's deployment.

Failure policy: fail-soft. Anything the proxy refuses is logged (never with a
secret in the message) and reported as ``None``/``False`` so the caller falls
back to the direct-provider path it used before.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)


# ─── Constants ───────────────────────────────────────────────────────

#: Credential-store `provider` tag under which a workspace's LiteLLM *virtual*
#: key is cached. It is deliberately NOT one of `src.llm.keys._ENV_FALLBACK`'s
#: providers — a virtual key is never a provider key and must never be
#: resolvable by `resolve_api_key`.
VIRTUAL_KEY_PROVIDER = "litellm_virtual"
_LABEL = "default"

#: Surfaces we provision deployments for (mirrors `profiles.PROFILE_NAMES`).
SURFACES: tuple[str, ...] = ("chat", "review", "embeddings", "agent")
_SURFACE_SUFFIX = {"chat": "chat", "review": "review", "embeddings": "embed",
                   "agent": "agent"}

#: Don't hammer a broken proxy: after a failed provisioning attempt, wait this
#: long before trying again (the caller keeps working off direct keys).
_RETRY_SECONDS = 60.0
#: Route lookups happen on every LLM call — cache the store read briefly.
_ROUTE_TTL_SECONDS = 30.0
#: Whole-provisioning budget. `provision_workspace` is called synchronously
#: from an async request handler, so a proxy that accepts connections but never
#: answers would otherwise stall the event loop for timeout × 7 admin calls.
#: Giving up early costs one tenant a gateway route; not giving up costs
#: everyone the API.
_PROVISION_BUDGET_SECONDS = 45.0

_LOCK = threading.Lock()
_route_cache: dict[str, tuple[float, _Record | None]] = {}
_attempts: dict[str, float] = {}

_PLACEHOLDERS = frozenset({"", "replace-me", "change-me", "sk-your-key-here"})


class EmbeddingConfigMismatch(RuntimeError):
    """The embeddings route no longer matches the vectors already indexed.

    Raised instead of silently embedding with a different model/width than the
    one the Qdrant collection was built with — that failure mode is invisible
    (search just gets worse), which is exactly why it has to be loud.
    """


# ─── Env switches ────────────────────────────────────────────────────


def proxy_url() -> str:
    """Base URL of the LiteLLM proxy, no trailing slash ("" when unset)."""
    return (os.environ.get("LITELLM_PROXY_URL") or "").strip().rstrip("/")


def master_key() -> str:
    """Proxy admin key. Only ever used for /team, /model and /key admin calls."""
    return (os.environ.get("LITELLM_MASTER_KEY") or "").strip()


def _timeout() -> float:
    try:
        return float(os.environ.get("LITELLM_PROXY_TIMEOUT") or 20.0)
    except (TypeError, ValueError):
        return 20.0


_warned_bad_prefix = False


def _sync_sdk_env() -> None:
    """Mirror our URL into ``LITELLM_PROXY_API_BASE``.

    LiteLLM's SDK reads that variable for ``litellm_proxy/…`` calls that pass
    only model+api_key — e.g. the deps report in :mod:`src.deps.report`, which
    gets routed for free through :class:`~src.llm.profiles.Profile` and has no
    idea a proxy exists. Without it the SDK raises "api_base not set".
    """
    url = proxy_url()
    if url and os.environ.get("LITELLM_PROXY_API_BASE") != url:
        os.environ["LITELLM_PROXY_API_BASE"] = url


def is_enabled() -> bool:
    """True when the LiteLLM gateway is configured and should be used.

    Pure env check — no I/O — because it runs on the hot path of every profile
    resolution.
    """
    global _warned_bad_prefix
    key = master_key()
    if not proxy_url() or not key or key in _PLACEHOLDERS or len(key) < 8:
        return False
    if not key.startswith("sk-"):
        # The proxy itself rejects a master key without the prefix; refusing
        # here turns a confusing 401-on-every-call into "gateway simply off".
        if not _warned_bad_prefix:
            _warned_bad_prefix = True
            logger.warning(
                "litellm_master_key_bad_prefix — LITELLM_MASTER_KEY must start "
                "with 'sk-'; gateway disabled"
            )
        return False
    _sync_sdk_env()
    return True


# ─── Naming ──────────────────────────────────────────────────────────


def _slug(value: str) -> str:
    """Proxy-safe, collision-resistant slug for a workspace id."""
    raw = (value or "default").strip()
    s = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if s != raw.lower() or len(s) > 40:
        # Two different ids must never collapse onto the same deployment name —
        # that would point one tenant at another tenant's provider key.
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        s = f"{s[:24].strip('-')}-{digest}" if s else digest
    return s or "default"


def team_id(workspace_id: str) -> str:
    """LiteLLM team id for a workspace (``/team/new`` accepts our own id)."""
    return f"ws-{_slug(workspace_id)}"


def deployment_name(workspace_id: str, surface: str) -> str:
    """``model_name`` of the proxy deployment backing one surface."""
    return f"celmis-{_slug(workspace_id)}-{_SURFACE_SUFFIX.get(surface, surface)}"


def qualify_model(provider: str, model: str) -> str:
    """LiteLLM-qualified model string (``gemini/gemini-3-flash-preview``)."""
    model = (model or "").strip()
    if not model or "/" in model:
        return model
    from src.llm.profiles import litellm_prefix
    return f"{litellm_prefix((provider or '').lower())}/{model}"


# ─── Secret-safe logging ─────────────────────────────────────────────

_SCRUB = (
    (re.compile(r'(?i)("?(?:api[_-]?key|key|token|password)"?\s*[:=]\s*"?)[^"\s,}]+'), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}"), "sk-***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{6,}"), "AIza***"),
)


def _safe(text: str, limit: int = 240) -> str:
    """Scrub anything key-shaped out of a proxy response before logging it.

    LiteLLM error bodies happily echo the request back — including
    ``litellm_params.api_key`` — so nothing from the wire is logged raw.
    """
    out = (text or "").replace("\n", " ")
    for pattern, repl in _SCRUB:
        out = pattern.sub(repl, out)
    return out[:limit]


# ─── HTTP plumbing ───────────────────────────────────────────────────


class _Resp(NamedTuple):
    status: int          # 0 == transport failure (proxy unreachable)
    data: Any            # parsed JSON body, or None
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _proxy_host() -> tuple[str, ...]:
    """The proxy's own host, as a one-item exception to the egress allowlist.

    Every call in this module goes to exactly one place: the URL the operator
    put in ``LITELLM_PROXY_URL``. In real deployments that is a compose
    service name or loopback — never on a shipped public allowlist, and
    refused outright while ``egress_allow_private_network`` is off (it is, by
    default, because a client that trusts every private address is an SSRF
    hole). Naming that one host here is what let this module move off a raw
    ``httpx.Client`` without changing what a configured gateway can reach:
    the proxy still answers, and a redirect to anywhere else now does not.
    """
    host = urlsplit(proxy_url()).hostname or ""
    return (host,) if host else ()


def _call(method: str, path: str, payload: dict | None = None) -> _Resp:
    base = proxy_url()
    if not base:
        return _Resp(0, None, "")
    headers = {
        "Authorization": f"Bearer {master_key()}",
        "Content-Type": "application/json",
    }
    # Imported here, like every other `src.` import in this module: the
    # gateway is resolved on the hot path of every profile lookup and must not
    # drag the security package in at import time.
    from src.http import build_client
    from src.security.egress import EgressBlockedError

    try:
        with build_client(
            timeout=_timeout(), extra_allowed_hosts=_proxy_host(),
        ) as client:
            resp = client.request(method, f"{base}{path}", json=payload, headers=headers)
    except EgressBlockedError as exc:
        # Distinct from "unreachable" on purpose: this one is a configuration
        # answer, not a network condition. Retrying will never fix it, and the
        # operator needs to see the word egress to know which knob to turn.
        logger.warning("litellm_gateway_egress_blocked path=%s err=%s", path, exc)
        return _Resp(0, None, "")
    except Exception as exc:  # noqa: BLE001 — any transport problem = fall back
        logger.warning(
            "litellm_gateway_unreachable path=%s err=%s", path, type(exc).__name__,
        )
        return _Resp(0, None, "")
    text = ""
    data: Any = None
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001
        text = ""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = None
    if not 200 <= resp.status_code < 300:
        logger.warning(
            "litellm_gateway_http path=%s status=%s detail=%s",
            path, resp.status_code, _safe(text),
        )
    return _Resp(int(resp.status_code), data, text)


def health() -> bool:
    """Liveliness probe — used by ops/diagnostics, never on the hot path."""
    if not is_enabled():
        return False
    return _call("GET", "/health/liveliness").ok


# ─── Provisioning records (credential-store cache) ───────────────────


@dataclass(frozen=True)
class _Record:
    """What we remember about a provisioned workspace."""

    key: str                       # the virtual key (secret)
    signature: str                 # fingerprint of what it was provisioned for
    deployments: dict[str, str]    # surface → model_name on the proxy
    models: dict[str, str]         # surface → underlying litellm model
    providers: dict[str, str]      # surface → provider slug


@dataclass(frozen=True)
class GatewayRoute:
    """Everything a caller needs to send one surface through the proxy."""

    workspace_id: str
    surface: str
    deployment: str                # model_name registered on the proxy
    virtual_key: str
    base_url: str
    underlying_model: str          # litellm model the deployment was built from
    provider: str


class _Entry(NamedTuple):
    surface: str
    provider: str
    model: str                     # already qualified ("gemini/…")
    api_key: str                   # the tenant's REAL provider key


def _store():
    from src.credentials import get_credential_store
    return get_credential_store()


def _slot(workspace_id: str) -> str:
    from src.llm.keys import workspace_slot
    return workspace_slot(workspace_id or "default")


def _load_record(workspace_id: str) -> _Record | None:
    try:
        stored = _store().load(
            provider=VIRTUAL_KEY_PROVIDER,
            user_id=_slot(workspace_id),
            account_label=_LABEL,
        )
    except Exception as exc:  # noqa: BLE001 — a corrupted row must not break calls
        logger.warning(
            "litellm_virtual_key_load_failed workspace=%s err=%s",
            workspace_id, type(exc).__name__,
        )
        return None
    if stored is None or not stored.secret:
        return None
    meta = stored.metadata or {}
    if meta.get("base_url") and meta.get("base_url") != proxy_url():
        # The proxy moved — the cached key belongs to a different deployment.
        return None
    return _Record(
        key=stored.secret,
        signature=str(meta.get("signature") or ""),
        deployments=dict(meta.get("deployments") or {}),
        models=dict(meta.get("models") or {}),
        providers=dict(meta.get("providers") or {}),
    )


def _save_record(workspace_id: str, record: _Record) -> None:
    _store().save(
        provider=VIRTUAL_KEY_PROVIDER,
        secret=record.key,
        metadata={
            "signature": record.signature,
            "deployments": record.deployments,
            "models": record.models,
            "providers": record.providers,
            "team_id": team_id(workspace_id),
            "base_url": proxy_url(),
            "provisioned_at": datetime.now(UTC).isoformat(),
            "saved_via": "llm_gateway",
        },
        user_id=_slot(workspace_id),
        account_label=_LABEL,
    )
    _route_cache.pop(workspace_id or "default", None)


def reset_cache(workspace_id: str | None = None) -> None:
    """Drop cached routes (call after provisioning or a profile/key change)."""
    if workspace_id is None:
        _route_cache.clear()
        _attempts.clear()
    else:
        _route_cache.pop(workspace_id or "default", None)
        _attempts.pop(workspace_id or "default", None)


def _cached_record(workspace_id: str) -> _Record | None:
    ws = workspace_id or "default"
    hit = _route_cache.get(ws)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _ROUTE_TTL_SECONDS:
        return hit[1]
    record = _load_record(ws)
    _route_cache[ws] = (now, record)
    return record


def _signature(entries: list[_Entry]) -> str:
    """Fingerprint of a provisioning plan.

    Includes a digest of each real provider key: rotating a key must produce a
    new deployment, otherwise the proxy keeps calling the provider with the
    revoked one.
    """
    parts = [proxy_url()]
    for e in sorted(entries, key=lambda x: x.surface):
        kh = hashlib.sha256(e.api_key.encode("utf-8")).hexdigest()[:16] if e.api_key else "none"
        parts.append(f"{e.surface}|{e.provider}|{e.model}|{kh}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


# ─── Proxy admin calls ───────────────────────────────────────────────


def _team_exists(tid: str) -> bool:
    """Whether `tid` is actually registered on the proxy (``GET /team/info``)."""
    return _call("GET", f"/team/info?team_id={quote(tid, safe='')}").ok


def _ensure_team(workspace_id: str) -> bool:
    """Create the workspace's team; an existing team is success, not an error."""
    tid = team_id(workspace_id)
    resp = _call("POST", "/team/new", {
        "team_id": tid,
        "team_alias": f"celmis:{workspace_id}",
        "metadata": {"celmis_workspace_id": workspace_id},
    })
    if resp.ok:
        return True
    if resp.status in (400, 409):
        # LiteLLM answers 400 both for "Team id = … already exists" (our
        # idempotent happy path) and for a request it rejected outright, with
        # nothing but the message to tell them apart. Ask instead of guessing:
        # taking a rejection for "already exists" would mint the virtual key
        # against a team_id that owns nothing, silently losing the per-tenant
        # spend grouping the team is there for.
        return _team_exists(tid)
    return False


def _existing_deployments() -> dict[str, list[str]] | None:
    """model_name → [model ids] currently registered, or None if unknown.

    None is not "none registered": ``/model/new`` APPENDS, so provisioning
    without a reliable picture of what is already there stacks a duplicate
    deployment that round-robins between the fresh provider key and the stale
    one. Callers must treat None as "abort", never as an empty dict.
    """
    resp = _call("GET", "/model/info")
    if not resp.ok:
        return None
    rows = (resp.data or {}).get("data") if isinstance(resp.data, dict) else None
    if rows is None:
        return None
    out: dict[str, list[str]] = {}
    for row in rows:
        try:
            name = str(row.get("model_name") or "")
            mid = str((row.get("model_info") or {}).get("id") or "")
        except AttributeError:
            continue
        if name and mid:
            out.setdefault(name, []).append(mid)
    return out


def _upsert_deployment(
    name: str, entry: _Entry, workspace_id: str, existing: dict[str, list[str]],
) -> bool:
    """Register (or replace) one deployment. Delete-then-create keeps it
    idempotent — /model/new appends, so a repeat call would otherwise stack
    duplicates that round-robin between stale and fresh provider keys."""
    for mid in existing.get(name, []):
        _call("POST", "/model/delete", {"id": mid})
    resp = _call("POST", "/model/new", {
        "model_name": name,
        "litellm_params": {"model": entry.model, "api_key": entry.api_key},
        "model_info": {
            "celmis_workspace_id": workspace_id,
            "celmis_surface": entry.surface,
            "celmis_provider": entry.provider,
            **({"mode": "embedding"} if entry.surface == "embeddings" else {}),
        },
    })
    return resp.ok


def _generate_key(workspace_id: str, models: list[str]) -> str | None:
    """Mint a virtual key restricted to exactly `models`."""
    allowed = [m for m in dict.fromkeys(models) if m]
    if not allowed:
        # An empty `models` on LiteLLM means "all models" — i.e. every other
        # tenant's deployment. Never generate one.
        logger.error(
            "litellm_refused_unrestricted_key workspace=%s — no deployments to "
            "scope the key to", workspace_id,
        )
        return None
    resp = _call("POST", "/key/generate", {
        "models": allowed,
        "team_id": team_id(workspace_id),
        "key_alias": f"celmis-{_slug(workspace_id)}-{uuid.uuid4().hex[:8]}",
        "metadata": {"celmis_workspace_id": workspace_id},
    })
    if not resp.ok:
        return None
    key = (resp.data or {}).get("key") if isinstance(resp.data, dict) else None
    if not isinstance(key, str) or not key.strip():
        logger.warning("litellm_key_generate_no_key workspace=%s", workspace_id)
        return None
    return key.strip()


def _delete_key(key: str) -> bool:
    return _call("POST", "/key/delete", {"keys": [key]}).ok


# ─── Public API ──────────────────────────────────────────────────────


def _forget(workspace_id: str) -> None:
    """Erase the stored provisioning so the workspace falls back to direct keys.

    Used when provisioning dies half-way: the cached record would still name
    deployments we have already deleted, and a route that points at a model the
    proxy no longer has fails EVERY call instead of degrading.
    """
    try:
        _store().delete(
            provider=VIRTUAL_KEY_PROVIDER, user_id=_slot(workspace_id),
            account_label=_LABEL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "litellm_stale_record_delete_failed workspace=%s err=%s",
            workspace_id, type(exc).__name__,
        )
    reset_cache(workspace_id)


def _provision(
    workspace_id: str, entries: list[_Entry], previous: _Record | None,
) -> str | None:
    """Create team + deployments + a scoped virtual key. None on any failure."""
    deadline = time.monotonic() + _PROVISION_BUDGET_SECONDS
    if not _ensure_team(workspace_id):
        logger.warning("litellm_team_provision_failed workspace=%s", workspace_id)
        return None

    existing = _existing_deployments()
    if existing is None:
        logger.warning(
            "litellm_model_list_unavailable workspace=%s — refusing to provision "
            "blind (would stack duplicate deployments)", workspace_id,
        )
        return None
    deployments: dict[str, str] = {}
    for entry in sorted(entries, key=lambda e: e.surface):
        name = deployment_name(workspace_id, entry.surface)
        if time.monotonic() > deadline:
            logger.warning(
                "litellm_provision_timed_out workspace=%s surface=%s",
                workspace_id, entry.surface,
            )
            _forget(workspace_id)
            return None
        if not _upsert_deployment(name, entry, workspace_id, existing):
            logger.warning(
                "litellm_model_provision_failed workspace=%s surface=%s",
                workspace_id, entry.surface,
            )
            # The delete half of the upsert has already run, so `previous`'s
            # deployment for this surface is gone.
            _forget(workspace_id)
            return None
        deployments[entry.surface] = name

    key = _generate_key(workspace_id, sorted(deployments.values()))
    if not key:
        return None

    # Only revoke the old key once the new one exists — a failed rotation must
    # not leave the tenant with no key at all.
    if previous is not None and previous.key and previous.key != key:
        _delete_key(previous.key)

    record = _Record(
        key=key,
        signature=_signature(entries),
        deployments=deployments,
        models={e.surface: e.model for e in entries},
        providers={e.surface: e.provider for e in entries},
    )
    try:
        _save_record(workspace_id, record)
    except Exception as exc:  # noqa: BLE001
        # We'd re-mint a key on every call otherwise — better to fall back.
        logger.warning(
            "litellm_virtual_key_save_failed workspace=%s err=%s",
            workspace_id, type(exc).__name__,
        )
        return None
    logger.info(
        "litellm_workspace_provisioned workspace=%s surfaces=%s",
        workspace_id, ",".join(sorted(deployments)),
    )
    return key


def ensure_workspace_keys(
    workspace_id: str,
    provider: str,
    real_api_key: str,
    models: dict[str, str],
) -> str | None:
    """Idempotently provision `workspace_id` on the proxy; return its virtual key.

    ``models`` maps surface → model (bare or already litellm-qualified). All
    surfaces share one `provider`/`real_api_key`; use :func:`provision_workspace`
    when each surface has its own provider.

    Returns None (and logs) whenever the proxy can't be used, so the caller
    keeps working off the tenant's direct provider key.
    """
    if not is_enabled():
        return None
    if (provider or "").lower() == "openai_compatible":
        # Same refusal as `_plan`: a proxy deployment for this provider would
        # carry "openai/<model>" without an api_base — i.e. api.openai.com.
        logger.warning(
            "litellm_provision_refused workspace=%s reason=self_hosted_profile",
            workspace_id,
        )
        return None
    if not (real_api_key or "").strip():
        logger.debug(
            "litellm_provision_skipped workspace=%s reason=no_provider_key",
            workspace_id,
        )
        return None
    entries = [
        _Entry(surface=s, provider=provider, model=qualify_model(provider, m),
               api_key=real_api_key)
        for s, m in sorted((models or {}).items())
        if s in SURFACES and (m or "").strip()
    ]
    if not entries:
        # No deployments → the only key we could mint would be unrestricted.
        logger.warning(
            "litellm_provision_skipped workspace=%s reason=no_models", workspace_id,
        )
        return None

    with _LOCK:
        previous = _load_record(workspace_id)
        signature = _signature(entries)
        if previous is not None and previous.key and previous.signature == signature:
            return previous.key
        return _provision(workspace_id, entries, previous)


def _plan(workspace_id: str) -> list[_Entry]:
    """Provisioning plan built from the workspace's own per-surface profiles."""
    from src.llm.profiles import PROFILE_NAMES, resolve_profile

    entries: list[_Entry] = []
    for surface in PROFILE_NAMES:
        try:
            p = resolve_profile(surface, workspace_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("litellm_plan_profile_failed surface=%s err=%s", surface, exc)
            continue
        if p.provider == "openai_compatible":
            # A self-hosted surface is never provisioned. The deployment we
            # would create carries litellm_params of model="openai/<model>"
            # plus the key — and NO api_base — so the proxy would forward this
            # tenant's prompts and code to api.openai.com. The key check below
            # does not catch it: a keyless local server resolves to the
            # "local-no-key" sentinel, which is non-empty precisely because
            # local servers need no key, and must not make the surface
            # provisioning-eligible. Second, independent layer of the refusal
            # in profiles._attach_gateway (belt and braces, like the other
            # fail-closed pairs in this codebase).
            logger.debug(
                "litellm_plan_skipped surface=%s workspace=%s "
                "reason=self_hosted_profile", surface, workspace_id,
            )
            continue
        # `raw_api_key` — never `api_key`: the latter is already the virtual key
        # when a (possibly stale) route is attached.
        key = (p.raw_api_key or "").strip()
        if not key or not p.model:
            continue
        entries.append(_Entry(
            surface=surface, provider=p.provider,
            model=qualify_model(p.provider, p.model), api_key=key,
        ))
    return entries


def provision_workspace(workspace_id: str = "default") -> bool:
    """Ensure every surface of `workspace_id` is routed through the proxy.

    Unlike :func:`ensure_workspace_keys` this allows a different provider per
    surface (chat on Anthropic, review on OpenAI, …) — they all end up behind a
    single virtual key scoped to that tenant's deployments.

    Never raises; a failed attempt is retried at most once a minute so a broken
    proxy costs one request's latency, not every request's.
    """
    if not is_enabled():
        return False
    ws = workspace_id or "default"
    with _LOCK:
        entries = _plan(ws)
        if not entries:
            return False
        signature = _signature(entries)
        previous = _load_record(ws)
        if previous is not None and previous.key and previous.signature == signature:
            return True
        now = time.monotonic()
        if now - _attempts.get(ws, float("-inf")) < _RETRY_SECONDS:
            return False
        _attempts[ws] = now
        return _provision(ws, entries, previous) is not None


def route_for(surface: str, workspace_id: str = "default") -> GatewayRoute | None:
    """Cached route for one surface, or None when the workspace isn't provisioned.

    Hot path: no HTTP, and at most one credential-store read per 30s.
    """
    if not is_enabled():
        return None
    ws = workspace_id or "default"
    record = _cached_record(ws)
    if record is None:
        return None
    deployment = record.deployments.get(surface)
    if not deployment or not record.key:
        return None
    return GatewayRoute(
        workspace_id=ws,
        surface=surface,
        deployment=deployment,
        virtual_key=record.key,
        base_url=proxy_url(),
        underlying_model=record.models.get(surface, ""),
        provider=record.providers.get(surface, ""),
    )


def revoke_workspace_key(workspace_id: str = "default") -> bool:
    """Delete the workspace's virtual key + deployments and forget the cache.

    True when something was actually revoked. Safe to call when the gateway is
    off (it still clears our local cache).
    """
    ws = workspace_id or "default"
    record = _load_record(ws)
    revoked = False
    if is_enabled():
        if record is not None and record.key:
            revoked = _delete_key(record.key)
        # None here means /model/info failed; there is nothing to enumerate, so
        # the deployments are left for the next revoke/provision to clean up.
        existing = _existing_deployments() or {}
        for surface in SURFACES:
            for mid in existing.get(deployment_name(ws, surface), []):
                _call("POST", "/model/delete", {"id": mid})
    try:
        _store().delete(
            provider=VIRTUAL_KEY_PROVIDER, user_id=_slot(ws), account_label=_LABEL,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "litellm_virtual_key_delete_failed workspace=%s err=%s",
            ws, type(exc).__name__,
        )
    reset_cache(ws)
    if revoked:
        logger.info("litellm_workspace_revoked workspace=%s", ws)
    return revoked


# ─── Embeddings safety net ───────────────────────────────────────────


def _indexed_signature() -> str | None:
    """`provider:model:dimensions` the Qdrant collection was last built with."""
    try:
        from src.api.routers.llm import _load_workspace_config
        raw = (_load_workspace_config("default") or {}).get("embeddings_indexed_signature")
    except Exception as exc:  # noqa: BLE001
        logger.debug("indexed_signature_unavailable err=%s", exc)
        return None
    return str(raw) if raw else None


def assert_embeddings_compatible(profile: Any, route: GatewayRoute | None = None) -> None:
    """Refuse to embed through the proxy with a drifted model/width.

    Two ways the gateway can silently poison the vector index:

      1. the deployment was provisioned from a different model than the one the
         embeddings profile now names (partial re-provisioning), and
      2. the profile itself drifted away from what the Qdrant collection was
         built with (someone switched model/dimensions but never re-indexed).

    Both produce vectors that are *valid* and *wrong* — search quality just
    degrades — so they raise :class:`EmbeddingConfigMismatch` instead.
    """
    want = qualify_model(profile.provider, profile.model)
    if route is not None and route.underlying_model and route.underlying_model != want:
        raise EmbeddingConfigMismatch(
            f"LiteLLM deployment {route.deployment!r} was provisioned for "
            f"{route.underlying_model!r} but the embeddings profile is now "
            f"{want!r}. Re-save the embeddings profile (re-provisions the "
            f"gateway) and re-index, or the vectors will not match the "
            f"collection."
        )
    indexed = _indexed_signature()
    if not indexed:
        return
    current = f"{profile.provider}:{profile.model}:{profile.dimensions}"
    if indexed == current:
        return
    parts = indexed.split(":")
    idx_dims = parts[-1] if parts else "?"
    raise EmbeddingConfigMismatch(
        f"embeddings profile is {current!r} but the vector collection was built "
        f"with {indexed!r} (width {idx_dims}). Searching would silently degrade. "
        f"Run 'Re-index embeddings' on LLM Setup, or switch the profile back."
    )


__all__ = [
    "EmbeddingConfigMismatch",
    "GatewayRoute",
    "SURFACES",
    "VIRTUAL_KEY_PROVIDER",
    "assert_embeddings_compatible",
    "deployment_name",
    "ensure_workspace_keys",
    "health",
    "is_enabled",
    "master_key",
    "provision_workspace",
    "proxy_url",
    "qualify_model",
    "reset_cache",
    "revoke_workspace_key",
    "route_for",
    "team_id",
]
