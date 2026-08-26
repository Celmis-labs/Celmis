"""LLM profiles — per-surface provider+model selection, UI-driven (Stage 22.1).

Three independent profiles, each pick their own provider + model in the UI
(/settings/llm); provider API keys are shared (one key per provider):

  * ``chat``       — Q&A / vault-generation answers (streamed)
  * ``review``     — PR-review agents
  * ``embeddings`` — vector-search embeddings (default Gemini; switching
                     provider/model requires a full re-index)

Storage:
  * profiles → workspace LLM config blob (``profiles`` key), reusing
    :func:`src.api.routers.llm._load_workspace_config`.
  * provider keys → credentials store, workspace-scoped (one row per provider).
    Google/Gemini falls back to the env ``GEMINI_API_KEY`` bootstrap.

Nothing here raises to the caller — a missing config degrades to sane
defaults so a fresh install works out of the box.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from src.config import get_settings

logger = logging.getLogger(__name__)

#: "agent" is the Celmis agent's planner — the one call that reads a sentence
#: into a plan. It borrowed the chat profile until now, which is the wrong
#: default in both directions: a workspace that points chat at a strong,
#: expensive model pays that price for a JSON classification, and one that
#: points chat at something cheap gets a planner that mis-reads sentences.
PROFILE_NAMES = ("chat", "review", "embeddings", "agent")

# Provider slug → LiteLLM prefix (google uses the native Gemini client for the
# `chat`/`embeddings` surfaces, but LiteLLM prefix is `gemini` when routed).
_LITELLM_PREFIX = {
    "google": "gemini",
    "gemini": "gemini",
    "openai": "openai",
    "anthropic": "anthropic",
    "openrouter": "openrouter",
    "groq": "groq",
    "mistral": "mistral",
    # Self-hosted OpenAI-compatible server (vLLM, llama.cpp, LM Studio, …) —
    # same slug the embeddings seam uses in src/config.py. litellm speaks to it
    # in the "openai" dialect; the ADDRESS comes from Profile.api_base, never
    # from this prefix. Without that api_base, "openai/<model>" is a working
    # call to api.openai.com — see _attach_gateway for why that must never
    # happen implicitly.
    "openai_compatible": "openai",
}

# Sensible defaults per surface when nothing is configured yet.
_DEFAULTS = {
    "chat":       ("google", None),   # model None → settings.gemini_generation_model
    "review":     ("google", None),
    "embeddings": ("google", None),   # model None → settings.gemini_embedding_model
    "agent":      ("google", None),   # falls back to the chat model
}

_WORKSPACE_USER = "workspace"
_WORKSPACE_LABEL = "default"


def litellm_prefix(provider: str) -> str:
    """LiteLLM vendor prefix for a provider slug (``google`` → ``gemini``)."""
    return _LITELLM_PREFIX.get(provider, provider)


@dataclass(frozen=True)
class Profile:
    surface: str          # chat | review | embeddings
    provider: str         # google | openai | anthropic | openrouter | groq | mistral | openai_compatible
    model: str            # bare model id, e.g. "gemini-3-flash-preview"
    api_key: str          # key to call with — the LiteLLM virtual key when routed
    dimensions: int | None = None   # embeddings only
    # Self-hosted OpenAI-compatible profiles only: the server address every
    # call for this surface must go to. None for hosted providers — litellm
    # knows their addresses. For provider="openai_compatible" the model string
    # is "openai/<model>", so a call made WITHOUT this api_base does not fail —
    # it goes to api.openai.com, which is the one place a self-hosted profile
    # exists to avoid. Callers refuse rather than default (fail-closed).
    api_base: str | None = None

    # ── LiteLLM gateway routing (all None when the gateway is off) ──
    # `provider`/`model` stay the LOGICAL selection (that's what the UI shows
    # and what the spend ledger and pricing tables are keyed on); only the
    # transport changes.
    gateway_model: str | None = None       # deployment name on the proxy
    gateway_url: str | None = None         # proxy base URL
    gateway_underlying: str | None = None  # model the deployment was built from
    raw_api_key: str = ""                  # the tenant's real provider key

    @property
    def via_gateway(self) -> bool:
        """True when calls for this surface go through the LiteLLM proxy."""
        return bool(self.gateway_model and self.gateway_url and self.api_key)

    @property
    def google_family(self) -> bool:
        """The *configured* vendor is Google, regardless of transport."""
        return self.provider in ("google", "gemini")

    @property
    def is_google(self) -> bool:
        """Use the native ``GeminiClient``.

        False when routed through the gateway: the whole point of the proxy is
        that Google leaves through the same door as everyone else.
        """
        return not self.via_gateway and self.google_family

    @property
    def litellm_model(self) -> str:
        if self.via_gateway:
            # `litellm_proxy/<deployment>` — the SDK strips the prefix and posts
            # the bare deployment name to the proxy's OpenAI-compatible route.
            return f"litellm_proxy/{self.gateway_model}"
        prefix = litellm_prefix(self.provider)
        return f"{prefix}/{self.model}" if self.model else self.model

    def signature(self) -> str:
        import hashlib
        kh = hashlib.sha256(self.api_key.encode()).hexdigest()[:10] if self.api_key else "none"
        return f"{self.surface}:{self.provider}:{self.model}:{self.dimensions}:{kh}"


# ─── config blob helpers ─────────────────────────────────────────────


def _blob(workspace_id: str = "default") -> dict:
    try:
        from src.api.routers.llm import _load_workspace_config
        return _load_workspace_config(workspace_id) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("profiles_blob_load_failed err=%s", exc)
        return {}


def _profiles_map(blob: dict | None = None, workspace_id: str = "default") -> dict:
    blob = blob if blob is not None else _blob(workspace_id)
    profs = dict(blob.get("profiles") or {})
    # Back-compat: legacy top-level provider/model was the *review* profile.
    if "review" not in profs and (blob.get("provider") or blob.get("model")):
        profs["review"] = {"provider": blob.get("provider"), "model": blob.get("model")}
    return profs


def is_configured(surface: str, workspace_id: str = "default") -> bool:
    """Has this workspace actually chosen a provider/model for `surface`?

    The difference between "set to the default" and "never set", which matters
    for a surface added later: an unset `agent` profile must behave exactly as
    it did before the profile existed — as the chat one — rather than as a
    fourth thing to configure before the feature works again.
    """
    entry = _profiles_map(workspace_id=workspace_id).get(surface) or {}
    return bool(entry.get("provider") or entry.get("model"))


def get_provider_key(provider: str, workspace_id: str = "default") -> str:
    """Effective provider key for `workspace_id`, resolved through the canonical
    chain in :func:`src.llm.keys.resolve_api_key` (ws:{id} → env; legacy slots
    only for the default tenant). Returns "" instead of raising so callers can
    render a "not configured" state."""
    from src.llm.keys import LLMCredentialError, resolve_api_key

    # google/gemini are the same underlying key — try both aliases.
    candidates = ("google", "gemini") if provider in ("google", "gemini") else (provider,)
    for prov in candidates:
        try:
            return resolve_api_key(prov, workspace_id=workspace_id)
        except LLMCredentialError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("provider_key_resolve_failed provider=%s err=%s", prov, exc)
    return ""


def set_provider_key(provider: str, key: str, workspace_id: str = "default") -> None:
    from src.credentials import get_credential_store
    from src.llm.keys import workspace_slot
    store = get_credential_store()
    store.save(provider=provider, secret=key, metadata={"saved_via": "llm_profiles"},
               user_id=workspace_slot(workspace_id), account_label=_WORKSPACE_LABEL)


def resolve_profile(surface: str, workspace_id: str = "default") -> Profile:
    if surface not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {surface!r}")
    s = get_settings()
    # Embeddings are workspace-SHARED (one Qdrant collection): always resolve
    # the "default" tenant's embeddings profile + key, regardless of caller ws.
    effective_ws = "default" if surface == "embeddings" else workspace_id
    profs = _profiles_map(workspace_id=effective_ws)
    entry = profs.get(surface) or {}
    prov = (entry.get("provider") or _DEFAULTS[surface][0]).lower()
    model = entry.get("model") or _DEFAULTS[surface][1]
    # Fill model default from Settings for google surfaces.
    if not model:
        if surface == "embeddings":
            model = s.gemini_embedding_model
        elif surface in ("chat", "agent"):
            model = s.gemini_generation_model
        else:  # review
            model = s.gemini_generation_model
    dims = None
    if surface == "embeddings":
        dims = int(entry.get("dimensions") or s.gemini_embedding_dimensions)
    # Embeddings: the MODEL/dimensions stay shared (one Qdrant collection),
    # but the KEY may come from the calling workspace when the default tenant
    # has none — a BYOK tenant can then generate/search vectors with its own
    # key while writing into the shared collection.
    api_key = get_provider_key(prov, effective_ws)
    if not api_key and surface == "embeddings" and workspace_id != "default":
        api_key = get_provider_key(prov, workspace_id)
    # Self-hosted profiles carry their server address in the per-surface dict
    # ("base_url" in the UI). Hosted providers never have one.
    api_base = str(entry.get("base_url") or "").strip() or None
    profile = Profile(
        surface=surface, provider=prov, model=model,
        api_key=api_key, dimensions=dims, raw_api_key=api_key,
        api_base=api_base,
    )
    # Gateway routing is per CALLING workspace even for embeddings: the shared
    # model/dimensions come from the "default" tenant above (one Qdrant
    # collection), but each tenant keeps its own proxy deployment + virtual key
    # so no key is ever shared across tenants at the transport level.
    return _attach_gateway(profile, workspace_id)


def _attach_gateway(profile: Profile, workspace_id: str) -> Profile:
    """Point `profile` at the LiteLLM proxy when this workspace is provisioned.

    No-op (and a single env read) when the gateway is off, so the direct-key
    path stays byte-for-byte what it was. Never does HTTP — provisioning is
    triggered explicitly from :mod:`src.llm.completion`, so rendering the
    settings page can't block on the proxy.
    """
    if profile.provider == "openai_compatible":
        # NEVER attached, even when the gateway is on. The provisioning POST
        # (gateway._upsert_deployment) writes litellm_params with the model and
        # key but WITHOUT api_base, and this provider's model string is
        # "openai/<model>" — so a proxy deployment built from a self-hosted
        # profile would forward the workspace's prompts and code to
        # api.openai.com, authenticated with the "local-no-key" sentinel (or a
        # real local token, leaking that too). A self-hosted profile exists
        # precisely so traffic stays on the operator's network; deploying it
        # without an api_base means api.openai.com. gateway._plan refuses the
        # same profiles independently — two layers, like every other
        # fail-closed pair in this codebase.
        logger.debug(
            "gateway_attach_refused surface=%s provider=openai_compatible — "
            "self-hosted profiles are never routed through the LiteLLM proxy",
            profile.surface,
        )
        return profile
    try:
        from src.llm import gateway

        if not gateway.is_enabled():
            return profile
        route = gateway.route_for(profile.surface, workspace_id)
        if route is None:
            return profile
        return replace(
            profile,
            api_key=route.virtual_key,
            gateway_model=route.deployment,
            gateway_url=route.base_url,
            gateway_underlying=route.underlying_model,
        )
    except Exception as exc:  # noqa: BLE001 — routing must never break a call
        logger.warning("gateway_route_failed surface=%s err=%s", profile.surface, exc)
        return profile


def set_profile(surface: str, *, provider: str, model: str | None,
                dimensions: int | None = None, workspace_id: str = "default") -> None:
    if surface not in PROFILE_NAMES:
        raise ValueError(f"unknown profile {surface!r}")
    from src.api.routers.llm import _load_workspace_config, _save_workspace_config
    # Embeddings config is shared (see resolve_profile) — write it to the
    # default tenant so every workspace embeds into the same Qdrant collection.
    effective_ws = "default" if surface == "embeddings" else workspace_id
    blob = _load_workspace_config(effective_ws)
    profs = dict(blob.get("profiles") or {})
    entry = {"provider": provider, "model": model}
    if surface == "embeddings" and dimensions:
        entry["dimensions"] = int(dimensions)
    profs[surface] = entry
    blob["profiles"] = profs
    # Keep legacy fields mirrored for the review profile (back-compat).
    if surface == "review":
        blob["provider"] = provider
        blob["model"] = model
    _save_workspace_config(blob, updated_by="llm_profiles", workspace_id=effective_ws)


def embeddings_signature() -> str:
    """Fingerprint of the shared embeddings profile — used to detect that a
    re-index is needed after switching provider/model/dimensions."""
    p = resolve_profile("embeddings")  # always resolves the shared profile
    return f"{p.provider}:{p.model}:{p.dimensions}"
