"""Unified LLM configuration endpoint (Stage 11 — Kodus-style single-page setup).

Everything a tech-lead needs to configure the AI reviewer lives here:

    provider  — which LLM vendor (openai / anthropic / google / openrouter /
                groq / mistral / openai_compatible for a self-hosted server)
    model     — which model within that provider (LiteLLM-format string)
    api_key   — user's key (encrypted via Fernet in the credentials store)
    temperature       — model creativity, 0.0-1.0
    max_output_tokens — cap for each completion
    system_prompt_extras — optional workspace-wide append to every agent's
                           system prompt (short "always do X" bullets)

    GET  /api/llm/config             — read current config (key masked)
    PUT  /api/llm/config              — save
    POST /api/llm/test-connection    — verify {provider, key, [model]} works
                                       by pinging the provider's models list
                                       or making a 1-token completion.
    GET  /api/llm/local-setup-guide  — static instructions for pointing a
                                       surface at a self-hosted server.
    GET  /api/llm/model-capabilities — what the INSTALLED litellm knows about
                                       one model: its output ceiling, whether
                                       it reasons and in which vocabulary.

Per-agent overrides live in the same blob under an "agents" key —
{architect: {model?, max_output_tokens?, reasoning?}, security: {...}, ...} —
every field optional, absent meaning "inherit". They exist because the review
agents are not interchangeable: the architect reasons over a whole diff and
needs the room, the verifier answers yes/no. One workspace-wide ceiling had to
be set for the greediest agent and was then paid for by every call.

Storage: the provider API keys stay in the credentials store as before
(provider="openai"/"anthropic"/etc). The remaining config (model, temperature,
max_output_tokens, system_prompt_extras, agents) is stored as one JSON blob
under provider="__llm_workspace__", user_id="workspace" — no new tables.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.deps import (
    client_ip,
    current_workspace_id,
    get_current_user,
    require_workspace_admin,
)
from src.llm.keys import workspace_slot
from src.llm.prompts.language import normalise as resolve_docs_language
from src.security.audit import record_action
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/llm", tags=["llm"])


_WORKSPACE_PROVIDER_TAG = "__llm_workspace__"     # generic-KV row for config JSON
_WORKSPACE_USER = "workspace"
_WORKSPACE_LABEL = "default"

# Self-hosted OpenAI-compatible provider (Ollama / vLLM / llama.cpp / LM
# Studio). Same slug the embeddings seam uses in src/config.py — one name for
# "a server speaking the OpenAI dialect on the operator's own network".
_LOCAL_PROVIDER = "openai_compatible"
# Sentinel for keyless local servers. It exists ONLY to satisfy plumbing that
# insists on a non-empty key; code we control never sends it as a credential.
# (LiteLLM will pass it as a Bearer token on actual completions — local
# servers ignore auth, which is fine and documented; our own probes strip it.)
_LOCAL_SENTINEL_KEY = "local-no-key"
# The surfaces whose profile may carry a base_url. The agent planner is
# chat-shaped and goes through the same LiteLLM client as chat and review
# (build_llm_client → resolve_profile → api_base), so it takes one too.
# Embeddings are env-configured on purpose (see the seam note in get_config).
_BASE_URL_SURFACES = ("chat", "review", "agent")
# Providers LiteLLM can actually route EMBEDDINGS to — a strict subset of the
# chat vendors. litellm.embedding() has no anthropic or groq branch at all,
# and OpenRouter's catalog serves no embedding models, so a profile saved with
# one of those fails at index time, hours after the person who chose it left
# this page. Refuse at save time instead. openai_compatible is refused
# separately below, with the EMBEDDING_* env guidance.
_EMBEDDINGS_PROVIDERS = ("google", "gemini", "openai", "mistral")

# What one agent's entry may carry. Anything else is a 422: "max_tokens"
# instead of "max_output_tokens", saved silently, is the failure this whole
# surface exists to make impossible. WHICH agents may have an entry is
# src.review.settings.REVIEW_AGENTS — read through _agent_names() below, so
# there is one spelling of that set and it lives with the agents.
AGENT_FIELDS: tuple[str, ...] = (
    "model", "max_output_tokens", "reasoning", "temperature",
)

# Bounds for a per-agent output ceiling. Same numbers as the workspace-wide
# `LLMConfigIn.max_output_tokens` on purpose — one notion of "a sane ceiling"
# in this file, not two. A model's OWN ceiling is enforced on top of these
# whenever litellm knows the model (see _validate_agent_entry).
AGENT_TOKENS_MIN = 64
AGENT_TOKENS_MAX = 200_000


# ─── Schemas ─────────────────────────────────────────────────────────


class ProfileOut(BaseModel):
    provider: str
    model: str
    dimensions: int | None = None
    # Self-hosted (OpenAI-compatible) profiles only: the server address calls
    # for this surface go to. None for hosted providers.
    base_url: str | None = None


class EffectiveEmbeddingsOut(BaseModel):
    """What actually embeds, when EMBEDDING_PROVIDER overrides the UI profile.

    Read-only by design: indexing is the call that ships source code to the
    embedder, so where it goes is an installation-level (env) decision — see
    the seam comment above `_configured_embedder` in src/llm/completion.py.
    This block exists so the settings page reports the env-configured local
    embedder instead of showing a profile that is not what runs.
    """

    provider: str
    base_url: str
    model: str
    dimensions: int | None = None       # None → whatever width the server returns
    source: str = "env"                 # env is the only writer of this block


class ProviderKeyOut(BaseModel):
    provider: str
    connected: bool
    masked: str
    source: str                                 # "ui" | "env" | "none"


class AgentSettingsOut(BaseModel):
    """One review agent's overrides, plus what it actually ends up with.

    The three override fields are null when the workspace has not set them —
    null means "inherit", and the UI must render it as inheritance rather than
    as a zero. The `effective_*` pair is what a review would run with right
    now, resolved through the documented order (repo policy → this entry →
    the workspace review profile → ReviewSettings). It is reported because a
    settings page that shows only the overrides shows six empty boxes and
    cannot answer the one question the operator has: what is in force?
    """

    model: str | None = None
    max_output_tokens: int | None = None
    #: A reasoning-effort word ("low"/"high"/…) for a model that takes one, or
    #: a token budget for a model that takes an integer. Which of the two a
    #: model wants is what GET /model-capabilities reports as reasoning_kind.
    reasoning: str | int | None = None
    #: Sampling temperature. Null means inherit. Zero is a VALUE, not an
    #: absence — the most deterministic one — so the page must not render it
    #: as an empty box.
    temperature: float | None = None

    #: Resolved, read-only. `effective_model` is a LiteLLM model string
    #: (provider prefix included) so it can be handed straight back to
    #: GET /model-capabilities. Null on either of them means the chain ran out
    #: without an answer — a workspace with no model chosen and no env default
    #: — which the page must render as "unknown", never as a substituted
    #: number: a made-up ceiling is how the truncation bug got here.
    effective_model: str
    effective_max_output_tokens: int | None = None
    effective_reasoning: str | int | None = None
    effective_temperature: float | None = None


class LLMConfigOut(BaseModel):
    """Everything the /settings/llm page needs to render, in one payload.

    `api_key_masked` shows the last 4 chars only — enough to confirm which
    key is saved without ever exposing it. When there's no key we return
    an empty string.
    """

    provider: str | None                        # e.g. "anthropic"
    model: str | None                           # e.g. "anthropic/claude-sonnet-5"
    temperature: float
    max_output_tokens: int
    system_prompt_extras: str                   # appended to every agent's system prompt

    api_key_masked: str                         # "sk-...-xyz1" or ""
    api_key_connected: bool                     # true if a key is saved
    connection_last_verified: str | None        # ISO timestamp of last successful test

    # ── OpenRouter fallback (separate section on the UI) ──
    openrouter_enabled: bool                    # toggle — false hides fallback logic
    openrouter_key_masked: str
    openrouter_key_connected: bool

    # ── Per-surface profiles (chat / review / embeddings / agent) ──
    profiles: dict[str, ProfileOut]             # keys: chat | review | embeddings | agent
    # Review brain: 'api' (BYOK models pipeline) | 'claude_code' (subscription)
    review_engine: str = "api"
    # Language review comments are written in (BCP-47-ish code, e.g. "uk")
    review_language: str = "en"
    # The model a failing review agent retries on once the primary is
    # exhausted — at most one extra call, and only then. Born of a measured
    # afternoon: gemini-3.7-flash refusing 40% of agent calls while
    # gemini-3.6-flash refused none, so a same-vendor fallback is a real
    # remedy for a real outage mode, not a hypothetical. None = no fallback,
    # and none is the default: a fallback trades comparability between runs
    # (two runs of one PR may be judged by different models) for liveness,
    # and that trade is the operator's to make, never ours.
    review_fallback_model: str | None = None
    # Language the generated vault documentation is written in. Every
    # generation prompt used to hardcode "Пишеш українською" ("You write in
    # Ukrainian"), so a workspace whose people read German got Ukrainian docs
    # and had no way to say otherwise. Separate from review_language on
    # purpose: PR comments are read by outside contributors on GitHub,
    # documentation by the team.
    docs_language: str = "uk"
    # Which engine writes it: "api" (one prompt through the gateway) or
    # "claude_code" (an agent that researches through the Celmis index).
    docs_engine: str = "api"
    # ── Per-agent overrides (architect / security / quality / …) ──
    # Every known agent is present, so the UI never has to guess whether an
    # absent key means "inherit" or "this agent does not exist".
    agents: dict[str, AgentSettingsOut]
    provider_keys: list[ProviderKeyOut]         # shared keys, one per provider
    embeddings_reindex_needed: bool             # true if embeddings profile drifted
    # Set when EMBEDDING_PROVIDER=openai_compatible is active: the UI must
    # render THIS as the embeddings surface (read-only), not the profile above.
    effective_embeddings: EffectiveEmbeddingsOut | None = None


class LLMConfigIn(BaseModel):
    """Body for PUT /api/llm/config. `api_key` is optional — omit to keep
    the currently-stored key untouched."""

    provider: str | None = None
    model: str | None = None
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=4096, ge=64, le=200_000)
    system_prompt_extras: str = Field(default="", max_length=4000)
    api_key: str | None = Field(default=None, max_length=500)

    # OpenRouter fallback — separate key stored under provider="openrouter"
    openrouter_enabled: bool = False
    openrouter_api_key: str | None = Field(default=None, max_length=500)

    # Per-surface profiles: {chat: {provider, model, base_url?}, review: {...},
    # agent: {...}, embeddings: {provider, model, dimensions}} — only provided
    # ones updated. base_url (chat/review/agent only, http:// or https://
    # required) is the address of a self-hosted OpenAI-compatible server;
    # validated in the handler because the entries are untyped dicts.
    profiles: dict[str, dict] | None = None
    # Shared provider keys to save: {google: "AIza…", openai: "sk-…"}.
    provider_keys: dict[str, str] | None = None
    # Review engine selection (None = keep current).
    review_engine: str | None = Field(default=None, pattern="^(api|claude_code)$")
    # Review output language (None = keep current).
    review_language: str | None = Field(default=None, max_length=16)
    # Review fallback model. None = keep current (this PUT is a patch, see
    # `sent` below); "" = clear — empty means "no fallback" everywhere on
    # this surface. Validated in the handler against the primary this same
    # request saves, not the one it replaces.
    review_fallback_model: str | None = Field(default=None, max_length=200)
    # Documentation output language (None = keep current).
    docs_language: str | None = Field(default=None, max_length=16)
    docs_engine: str | None = Field(
        default=None, pattern="^(api|claude_code)$")
    # Per-agent overrides: {architect: {max_output_tokens: 32768,
    # reasoning: "high"}, verifier: {model: "gemini-3-flash-preview"}}.
    # Sent WHOLE and replacing the stored map, unlike `profiles` above, which
    # merges per surface. Absent means "inherit" at every layer of this chain,
    # so omitting an agent — or a field inside one — is the only way the form
    # can say "stop overriding that"; a merge would read that request as
    # "leave it alone". Not sending the key at all still leaves the stored map
    # untouched, so a partial PUT from a neighbouring card cannot wipe it.
    agents: dict[str, dict | None] | None = None


class TestConnectionIn(BaseModel):
    provider: str
    # Optional because the self-hosted provider is keyless by design; every
    # hosted provider still refuses without one — checked in the handler so
    # the refusal is a readable `detail`, not a bare 422.
    api_key: str | None = Field(default=None, min_length=8, max_length=500)
    model: str | None = None                    # optional — used for a 1-token ping
    # Self-hosted (OpenAI-compatible) only:
    base_url: str | None = Field(default=None, max_length=500)
    surface: str = Field(default="chat", pattern="^(chat|embeddings)$")


class TestConnectionOut(BaseModel):
    ok: bool
    provider: str
    detail: str                                 # human-readable status
    latency_ms: int | None = None
    models_available: int | None = None
    balance_usd: float | None = None            # OpenRouter surfaces credit balance
    # Self-hosted embeddings probe only — the width of the vector the server
    # actually returned. Structured, not just prose in `detail`: the UI
    # headlines it, because this is the number that must match the index.
    vector_width: int | None = None
    # A caution that is not a failure (e.g. the width differs from the
    # existing collection's) — rendered as a warning, apart from `detail`.
    warning: str | None = None


# ─── Helpers ─────────────────────────────────────────────────────────


def _mask_key(key: str) -> str:
    """Show only enough to identify the key — first 4 + last 4."""
    if not key or len(key) < 12:
        return "•" * len(key) if key else ""
    return f"{key[:4]}…{key[-4:]}"


def _validated_base_url(raw: object) -> str:
    """The base_url a profile may store: http(s), non-empty, no trailing slash.

    Anything else is refused rather than normalised — "ollama:11434" or a
    file:// URL saved now becomes a confusing connection error later, in a
    code path (a review run, indexing) with no user in front of it.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(
            status_code=422, detail="base_url must be a non-empty string",
        )
    url = raw.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=422,
            detail="base_url must start with http:// or https:// "
                   "(e.g. http://127.0.0.1:11434/v1)",
        )
    return url


def _load_workspace_config(workspace_id: str = "default") -> dict[str, Any]:
    """Load the LLM config JSON blob for `workspace_id`. Empty dict on first use."""
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError

    store = get_credential_store()
    try:
        row = store.load(
            provider=_WORKSPACE_PROVIDER_TAG,
            user_id=workspace_slot(workspace_id),
            account_label=_WORKSPACE_LABEL,
        )
    except CredentialStoreError:
        return {}
    if row is None:
        return {}
    try:
        return json.loads(row.secret)
    except (json.JSONDecodeError, ValueError):
        logger.warning("workspace_llm_config_corrupt — resetting to defaults")
        return {}


def _save_workspace_config(
    cfg: dict[str, Any], updated_by: str, workspace_id: str = "default",
) -> None:
    from src.credentials import get_credential_store

    store = get_credential_store()
    store.save(
        provider=_WORKSPACE_PROVIDER_TAG,
        secret=json.dumps(cfg),
        metadata={"updated_by": updated_by},
        user_id=workspace_slot(workspace_id),
        account_label=_WORKSPACE_LABEL,
    )


def _current_key(provider: str, workspace_id: str = "default") -> str | None:
    """Read a workspace's provider key row from the credentials store."""
    from src.credentials import get_credential_store
    from src.credentials.store import CredentialStoreError

    store = get_credential_store()
    try:
        row = store.load(
            provider=provider, user_id=workspace_slot(workspace_id),
            account_label="default",
        )
    except CredentialStoreError:
        return None
    if row is None:
        return None
    return row.secret


# ─── Per-agent configuration: capabilities in, overrides out ─────────
#
# The FACTS about a model (its output ceiling, whether it reasons and in
# which vocabulary) are not decided here. They are read out of the installed
# LiteLLM by src/llm/capabilities.py — one probe, shared by the resolver that
# builds the request and by this endpoint that renders the form, so the UI
# cannot offer a value the request path would silently drop.


class ProviderRefusalOut(BaseModel):
    """One value the provider refused for a model — `ProviderRefusal.as_dict()`."""

    parameter: Literal["reasoning", "temperature"]
    value: str
    reason: str
    seen_at: str


class ModelCapabilitiesOut(BaseModel):
    """The wire shape of GET /api/llm/model-capabilities.

    Mirrors :meth:`src.llm.capabilities.ModelCapabilities.as_dict` field for
    field; it is declared here so the OpenAPI schema the UI generates from is
    accurate, and built from that dict so the two cannot drift.

    ``known=False`` is a first-class answer, not an error. A self-hosted
    server (provider ``openai_compatible``) is addressed as
    ``openai/<whatever the operator called it>``, which LiteLLM has never
    heard of and never will; so is any model newer than the installed
    package. Everything else is null then and the UI says "unknown" rather
    than drawing a slider over an invented maximum.
    """

    model: str
    known: bool
    max_output_tokens: int | None = None
    supports_reasoning: bool | None = None
    reasoning_kind: Literal["effort", "budget"] | None = None
    #: The old name for `reasoning_values_router_accepts`, on its way out —
    #: see the property of that name on ModelCapabilities.
    reasoning_values: list[str] | None = None
    #: The effort words the ROUTER translates for this model, minus what the
    #: provider has since refused — and the refused ones, separately, so the
    #: settings page can explain a word it used to offer instead of silently
    #: losing it between two page loads.
    reasoning_values_router_accepts: list[str] | None = None
    reasoning_values_provider_refused: list[str] | None = None
    supports_function_calling: bool | None = None
    source: Literal["litellm", "unknown"] = "unknown"
    #: Everything the provider has refused for this model, learned from the
    #: calls that went out, with the sentence and WHEN it was learned. The
    #: facts behind `reasoning_values_provider_refused`, plus the one that
    #: list cannot hold — a temperature the model will not take — so the page
    #: can say "refused by the provider on <date>" and not just hide the
    #: option. Always a list; empty means nothing learned.
    provider_refusals: list[ProviderRefusalOut] = Field(default_factory=list)


def _agent_names() -> tuple[str, ...]:
    """The agents that may carry their own model and budget.

    ``REVIEW_AGENTS`` is the one spelling of that set; imported inside the
    function because src/review/orchestrator.py imports THIS module, and a
    module-level import back into src.review is a cycle waiting for the day
    somebody adds a line to src/review/__init__.py.
    """
    from src.review.settings import REVIEW_AGENTS
    return REVIEW_AGENTS


def _review_selection(cfg: dict[str, Any], workspace_id: str) -> tuple[str, str]:
    """(provider, bare model) the review surface is set to.

    Reads the blob it is handed rather than re-reading storage, so a PUT that
    changes the review profile validates its agents against the profile it is
    SAVING and not the one it is replacing.
    """
    entry = (cfg.get("profiles") or {}).get("review") or {}
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    if provider and model:
        return provider, model
    try:
        from src.llm.profiles import resolve_profile
        p = resolve_profile("review", workspace_id)
    except Exception:  # noqa: BLE001 — an unconfigured workspace still renders
        return provider, model
    # The LOGICAL selection, never `p.litellm_model`: with the LiteLLM gateway
    # on that is "litellm_proxy/<deployment>", a name only the proxy knows and
    # LiteLLM's model map never will — so every capability lookup would come
    # back unknown for a workspace that is perfectly well configured.
    return provider or p.provider, model or p.model


def _validated_review_fallback(cfg: dict[str, Any], workspace_id: str) -> str | None:
    """The review fallback model this save may keep — or the 422 saying why not.

    Judged against `cfg` as it will be AFTER the save (call this after the
    profiles merge), for the same reason `_agent_overrides_from_payload` does:
    a refusal in front of the operator must name the primary they are about
    to run, not the one they are replacing. That also means changing the
    PRIMARY to equal a stored fallback is refused here too — the pair is
    validated as a pair, whichever half the request carried.

    Identity is checked on the resolved LiteLLM string as well as the bare
    id, because "gemini/gemini-3-flash-preview" and "gemini-3-flash-preview"
    are one model in two spellings and a fallback that resolves to the
    primary retries the very model that just failed — a second failure
    bought at full price, which is the opposite of what the field is for.

    Beyond identity, the model goes through the SAME capability path the
    primary goes through — `resolve_litellm_model` + `model_capabilities` —
    and with the same acceptance rule: `known: false` is an answer, not an
    error. A self-hosted name is the ordinary case on this surface (see
    GET /model-capabilities), and the primary saves fine while unknown, so a
    stricter gate here would refuse installations the primary path serves.
    """
    raw = cfg.get("review_fallback_model")
    fallback = str(raw).strip() if isinstance(raw, str) else None
    if not fallback:
        return None

    from src.llm.capabilities import model_capabilities, resolve_litellm_model

    provider, primary = _review_selection(cfg, workspace_id)
    fallback_resolved = resolve_litellm_model(fallback, provider)
    if fallback == primary or (
        primary and fallback_resolved == resolve_litellm_model(primary, provider)
    ):
        raise HTTPException(status_code=422, detail=(
            f"review_fallback_model '{fallback}' is the review model itself. "
            "A fallback exists to answer when that exact model cannot, so "
            "retrying it buys a second failure, not a review. Pick a "
            "different model, or clear the field for no fallback."
        ))
    model_capabilities(fallback_resolved)
    return fallback


def _effective_agent(
    agent: str, cfg: dict[str, Any], workspace_id: str, *,
    policy: dict | None = None, selection: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """What `agent` would run with right now, as the UI needs to show it.

    The chain itself is :func:`src.review.settings.resolve_agent_llm` — the
    same call the orchestrator makes, so this page cannot claim one thing
    while a review does another. Only the model STRING differs: the resolver
    speaks in bare ids, and a settings page needs the form LiteLLM indexes by
    to hand back to GET /model-capabilities. That form comes from
    :func:`src.llm.capabilities.resolve_litellm_model` — the same function
    `LLMClient.generate` puts on the wire, so the two cannot drift.
    """
    from src.llm.capabilities import resolve_litellm_model
    from src.review.settings import resolve_agent_llm
    resolved = resolve_agent_llm(agent, policy=policy, workspace_cfg=cfg)
    provider, _model = selection or _review_selection(cfg, workspace_id)
    return {
        "model": resolve_litellm_model(resolved.model or "", provider),
        "max_output_tokens": resolved.max_output_tokens,
        "reasoning": resolved.reasoning,
        "temperature": resolved.temperature,
    }


def resolve_agent_settings(
    agent: str, workspace_id: str = "default", policy: dict | None = None,
) -> dict[str, Any]:
    """Public: one agent's effective {model, max_output_tokens, reasoning,
    temperature},
    with `model` as a LiteLLM string ready for :func:`model_capabilities`.

    A thin wrapper over :func:`src.review.settings.resolve_agent_llm` that
    supplies the workspace blob and the provider prefix. Callers that already
    hold the blob should use the resolver directly.
    """
    if agent not in _agent_names():
        raise ValueError(f"unknown review agent {agent!r}")
    return _effective_agent(
        agent, _load_workspace_config(workspace_id), workspace_id, policy=policy,
    )


def _validate_agent_entry(agent: str, entry: dict[str, Any], model_string: str) -> None:
    """Refuse a per-agent override that cannot do what it says.

    Everything here fails at SAVE time, in front of the person who chose it.
    The alternative is a queued review hours later answering a provider 400 —
    or worse, and the reason `reasoning` is checked at all, the value being
    dropped on the floor with nothing but a log line to show for it. A control
    that silently changes nothing is the exact failure this surface was built
    to end: `gemini_thinking_budget` sat in the UI for months, wired only into
    the native client, reaching no LiteLLM call.
    """
    from src.llm.capabilities import model_capabilities
    caps = model_capabilities(model_string)

    temperature = entry.get("temperature")
    if temperature is not None:
        # Не через `or`: 0.0 — законне значення, найдетермінованіше з можливих,
        # і саме те, яке проковтнула б перевірка на істинність.
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise HTTPException(status_code=422, detail=(
                f"agent '{agent}': temperature must be a number, got {temperature!r}"
            ))
        if not (0.0 <= float(temperature) <= 2.0):
            raise HTTPException(status_code=422, detail=(
                f"agent '{agent}': temperature must be between 0 and 2, "
                f"got {temperature}"
            ))

    tokens = entry.get("max_output_tokens")
    if tokens is not None:
        if isinstance(tokens, bool) or not isinstance(tokens, int):
            raise HTTPException(status_code=422, detail=(
                f"agent '{agent}': max_output_tokens must be a whole number of "
                f"tokens, got {tokens!r}"
            ))
        if not (AGENT_TOKENS_MIN <= tokens <= AGENT_TOKENS_MAX):
            raise HTTPException(status_code=422, detail=(
                f"agent '{agent}': max_output_tokens must be between "
                f"{AGENT_TOKENS_MIN} and {AGENT_TOKENS_MAX}, got {tokens}"
            ))
        if caps.known and caps.max_output_tokens and tokens > caps.max_output_tokens:
            raise HTTPException(status_code=422, detail=(
                f"agent '{agent}': {model_string} accepts at most "
                f"{caps.max_output_tokens} output tokens, asked for {tokens}"
            ))

    if "reasoning" not in entry:
        return
    reasoning = entry["reasoning"]
    if isinstance(reasoning, bool):
        raise HTTPException(status_code=422, detail=(
            f"agent '{agent}': reasoning must be an effort level or a token "
            f"budget, not true/false"
        ))

    if caps.reasoning_kind is None:
        # Either LiteLLM has no entry for this model (self-hosted, or newer
        # than the installed package) or it has one that advertises no
        # reasoning parameter. Both mean `reasoning_kwargs` would return {}
        # and the request would go out without it — so storing a value would
        # be a setting that reaches nothing. Refuse instead of pretending.
        why = (
            "LiteLLM has no entry for that model"
            if not caps.known else
            "that model advertises no reasoning parameter"
        )
        raise HTTPException(status_code=422, detail=(
            f"agent '{agent}': a reasoning level cannot be sent to "
            f"{model_string} — {why}, so LiteLLM would drop it and the setting "
            f"would change nothing. Leave it unset."
        ))

    if caps.reasoning_kind == "effort":
        allowed = list(caps.reasoning_values or ())
        if not isinstance(reasoning, str) or reasoning.strip() not in allowed:
            # A model that ALSO takes a raw budget (Anthropic, Gemini) is
            # still offered the word only: LiteLLM translates the word into
            # that vendor's native shape, and one vocabulary per model is what
            # keeps the stored value renderable from `reasoning_kind` alone.
            raise HTTPException(status_code=422, detail=(
                f"agent '{agent}': {model_string} supports reasoning "
                f"{', '.join(allowed) if allowed else '(nothing this build can name)'}"
                f" — got {reasoning!r}"
            ))
        return

    # kind == "budget": a token count, and no word means anything to it.
    value = reasoning
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())     # a number input still posts a string
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=422, detail=(
            f"agent '{agent}': {model_string} takes a reasoning BUDGET in "
            f"tokens, not {reasoning!r}"
        ))
    if not (0 <= value <= AGENT_TOKENS_MAX):
        raise HTTPException(status_code=422, detail=(
            f"agent '{agent}': a reasoning budget must be between 0 (no "
            f"thinking) and {AGENT_TOKENS_MAX} tokens, got {value}"
        ))
    entry["reasoning"] = value          # store the number, never "4096"


def _agent_overrides_from_payload(
    incoming: dict[str, dict | None], cfg: dict[str, Any], workspace_id: str,
) -> dict[str, dict[str, Any]]:
    """Validate a PUT's `agents` block into the map that replaces the stored one.

    The block is sent WHOLE, not as a patch, and that is the only shape that
    can express a removal: absent already means "inherit" at every layer of
    this chain, so an omitted agent — or an omitted field inside an agent —
    is how the UI says "stop overriding that". A per-key merge would read the
    same request as "leave it alone" and quietly keep a value the operator
    watched disappear from the form, which is the silent-no-op failure this
    whole surface was built to end. (The top-level PUT stays a patch: an
    `agents` key that is not sent at all leaves the stored map untouched.)

    Two passes, and the split is the point. Shaping the payload cannot depend
    on a model, but validating it can depend on NOTHING ELSE — and the model an
    agent ends up on is only known once the whole block has been shaped, because
    the block replaces the stored map wholesale. Resolving against `cfg` while
    its "agents" key still held the PREVIOUS map is how PUT {"architect":
    {"reasoning": "high"}} over a stored architect.model="gpt-4o" came back 422
    "a reasoning level cannot be sent to gpt-4o" — for a save after which the
    architect inherits the review profile's model and takes "high" happily.
    Fail-closed is not a licence to refuse in the name of a model the workspace
    is not going to use.
    """
    known = _agent_names()
    merged: dict[str, dict[str, Any]] = {}

    for name, entry in incoming.items():
        if name not in known:
            raise HTTPException(status_code=422, detail=(
                f"unknown agent '{name}' — the review agents are: "
                f"{', '.join(known)}"
            ))
        if entry is None:
            continue                    # null → no overrides, same as omitting it
        if not isinstance(entry, dict):
            raise HTTPException(status_code=422, detail=(
                f"agent '{name}' must be an object of overrides, or null to "
                f"clear them"
            ))
        unknown = sorted(k for k in entry if k not in AGENT_FIELDS)
        if unknown:
            raise HTTPException(status_code=422, detail=(
                f"agent '{name}': unknown field(s) {', '.join(unknown)} — "
                f"allowed: {', '.join(AGENT_FIELDS)}"
            ))
        cur: dict[str, Any] = {}
        for field in AGENT_FIELDS:
            value = entry.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue                # absent, null or blank → inherit this one
            cur[field] = value.strip() if isinstance(value, str) else value
        if cur:
            merged[name] = cur          # an empty entry is no entry, not "set to nothing"

    # Second pass, against the config as it will be AFTER this save. Each agent
    # is judged on the model it will END UP on — its own new override, or, when
    # the payload cleared that override, whatever it now inherits — so that a
    # refusal in front of the operator names the model they are about to run.
    after = {**cfg, "agents": merged}
    selection = _review_selection(after, workspace_id)
    for name, cur in merged.items():
        # `_effective_agent` walks the same chain the orchestrator walks and
        # returns the same litellm string `LLMClient.generate` will call with —
        # asking it here is what keeps "what the page refused" and "what the
        # review would have sent" the same question. It also mutates `cur`
        # (a budget posted as "4096" is stored as 4096), and `cur` is the very
        # dict `merged` hands back, so the coercion lands in what gets saved.
        _validate_agent_entry(
            name, cur,
            _effective_agent(name, after, workspace_id, selection=selection)["model"],
        )
    return merged


@router.get("/model-capabilities", response_model=ModelCapabilitiesOut)
def get_model_capabilities(
    model: str,
    user: User = Depends(get_current_user),
) -> ModelCapabilitiesOut:
    """What the installed LiteLLM knows about `model`.

    Not admin-gated: it reports a public model catalogue — no workspace
    state, no key. A model LiteLLM does not know answers `known: false` with
    nulls and a 200, not a 400: a self-hosted model string is the ordinary
    thing to ask about here, and the settings page still has to render.

    Cached in :mod:`src.llm.capabilities` (a settings page asks about every
    agent's model on every render, and `get_model_info` walks a 3000-entry
    map). The facts come from a table shipped inside the LiteLLM package, so
    the only thing that can change them is upgrading LiteLLM — a deploy,
    which restarts the process. That restart is the whole invalidation story;
    a test or a live swap can call `reset_capability_caches()`.
    """
    from src.llm.capabilities import model_capabilities
    return ModelCapabilitiesOut(**model_capabilities(model).as_dict())


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("/config", response_model=LLMConfigOut)
def get_config(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> LLMConfigOut:
    cfg = _load_workspace_config(workspace_id)

    provider = cfg.get("provider")
    key = _current_key(provider, workspace_id) if provider else None
    or_key = _current_key("openrouter", workspace_id)

    # Per-surface profiles + per-workspace provider keys.
    from src.llm.profiles import (
        PROFILE_NAMES,
        embeddings_signature,
        resolve_profile,
    )
    profiles_out: dict[str, ProfileOut] = {}
    used_providers: set[str] = set()
    for name in PROFILE_NAMES:
        p = resolve_profile(name, workspace_id)
        profiles_out[name] = ProfileOut(
            provider=p.provider, model=p.model, dimensions=p.dimensions,
            base_url=p.api_base,
        )
        used_providers.add(p.provider)

    keys_out: list[ProviderKeyOut] = []
    for prov in sorted({"google", "openai", "anthropic", "openrouter", "groq",
                        "mistral", _LOCAL_PROVIDER} | used_providers):
        from src.llm.profiles import get_provider_key
        if prov == _LOCAL_PROVIDER:
            # Keyless by design. A row may still exist (vLLM/TEI can require a
            # token, and the UI may save the sentinel to satisfy plumbing) —
            # mask a real token, never the sentinel: masking plumbing as if it
            # were a credential tells the operator a key exists when none does.
            wk = _current_key(prov, workspace_id) or ""
            real = wk if wk != _LOCAL_SENTINEL_KEY else ""
            keys_out.append(ProviderKeyOut(
                provider=prov, connected=bool(wk),
                masked=_mask_key(real) if real else "",
                source="ui" if wk else "none",
            ))
            continue
        wk = _current_key(prov, workspace_id) if prov not in ("google", "gemini") else None
        eff = get_provider_key(prov, workspace_id)
        if prov in ("google", "gemini"):
            src_ = "ui" if _current_key("google", workspace_id) else ("env" if eff else "none")
        else:
            src_ = "ui" if wk else "none"
        keys_out.append(ProviderKeyOut(
            provider=prov, connected=bool(eff),
            masked=_mask_key(eff) if eff else "", source=src_,
        ))

    # Per-agent overrides, plus what each agent actually ends up with. The
    # review profile resolved just above is reused as the provider prefix, so
    # this costs no second trip through resolve_profile per agent.
    review_selection = (profiles_out["review"].provider, profiles_out["review"].model)
    stored_agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    agents_out: dict[str, AgentSettingsOut] = {}
    for agent_name in _agent_names():
        entry = stored_agents.get(agent_name)
        entry = entry if isinstance(entry, dict) else {}
        effective = _effective_agent(
            agent_name, cfg, workspace_id, selection=review_selection,
        )
        agents_out[agent_name] = AgentSettingsOut(
            model=entry.get("model"),
            max_output_tokens=entry.get("max_output_tokens"),
            reasoning=entry.get("reasoning"),
            temperature=entry.get("temperature"),
            effective_model=effective["model"],
            effective_max_output_tokens=effective["max_output_tokens"],
            effective_reasoning=effective["reasoning"],
            effective_temperature=effective.get("temperature"),
        )

    reindex_needed = cfg.get("embeddings_indexed_signature") not in (None, embeddings_signature()) \
        if cfg.get("embeddings_indexed_signature") else False

    # The embeddings SEAM is env-first: EMBEDDING_PROVIDER decides WHOSE SERVER
    # embeds before any profile is consulted (src/llm/completion.py). When it
    # is active the profile above is decorative — this page used to show
    # "google / gemini-embedding-2" while every vector came off the operator's
    # own box, and could not say otherwise. Report what actually runs,
    # read-only: embeddings stay env-configured on purpose, because a seam a
    # UI selection could override is a seam a regulated install cannot rely on.
    effective_embeddings: EffectiveEmbeddingsOut | None = None
    from src.config import get_settings
    settings = get_settings()
    if settings.embedding_provider != "gemini":
        effective_embeddings = EffectiveEmbeddingsOut(
            provider=settings.embedding_provider,
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions or None,
            source="env",
        )

    return LLMConfigOut(
        provider=provider,
        model=cfg.get("model"),
        temperature=float(cfg.get("temperature", 0.1)),
        max_output_tokens=int(cfg.get("max_output_tokens", 4096)),
        system_prompt_extras=cfg.get("system_prompt_extras", ""),
        api_key_masked=_mask_key(key) if key else "",
        api_key_connected=bool(key),
        connection_last_verified=cfg.get("connection_last_verified"),
        openrouter_enabled=bool(cfg.get("openrouter_enabled", False)),
        openrouter_key_masked=_mask_key(or_key) if or_key else "",
        openrouter_key_connected=bool(or_key),
        profiles=profiles_out,
        agents=agents_out,
        provider_keys=keys_out,
        review_engine=str(cfg.get("review_engine") or "api"),
        review_language=str(cfg.get("review_language") or "en"),
        review_fallback_model=str(cfg.get("review_fallback_model") or "").strip() or None,
        docs_language=resolve_docs_language(cfg.get("docs_language")),
        docs_engine=str(cfg.get("docs_engine") or "api"),
        embeddings_reindex_needed=reindex_needed,
        effective_embeddings=effective_embeddings,
    )


@router.put("/config", response_model=LLMConfigOut)
def put_config(
    payload: LLMConfigIn,
    request: Request,
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> LLMConfigOut:
    from src.credentials import get_credential_store

    store = get_credential_store()
    slot = workspace_slot(workspace_id)
    # Which providers this request set a key for. Collected as it goes and
    # audited once at the end: three separate blocks below can each write a
    # credential, and three separate audit rows for one request would make a
    # single form submission look like three events.
    keys_saved: list[str] = []
    # Persist the primary provider api_key first (if provided) — under the
    # workspace's OWN slot so it is both readable (get_config reads ws:{id})
    # and isolated from other tenants.
    if payload.api_key and payload.provider:
        store.save(
            provider=payload.provider,
            secret=payload.api_key,
            metadata={"saved_via": "llm_config"},
            user_id=slot,
            account_label="default",
        )
        keys_saved.append(payload.provider)
        logger.info(
            "llm_key_saved provider=%s workspace=%s user=%s",
            payload.provider, workspace_id, user.email,
        )
    # OpenRouter fallback key (separate row).
    if payload.openrouter_api_key:
        store.save(
            provider="openrouter",
            secret=payload.openrouter_api_key,
            metadata={"saved_via": "llm_config_fallback"},
            user_id=slot,
            account_label="default",
        )
        keys_saved.append("openrouter")
        logger.info("openrouter_key_saved workspace=%s user=%s", workspace_id, user.email)

    # Per-surface provider keys (workspace-scoped, one per provider).
    if payload.provider_keys:
        from src.llm.profiles import set_provider_key
        for prov, k in payload.provider_keys.items():
            if k:
                set_provider_key(prov, k, workspace_id)
                keys_saved.append(prov)
                logger.info("provider_key_saved provider=%s workspace=%s user=%s", prov, workspace_id, user.email)

    prev = _load_workspace_config(workspace_id)

    # PATCH semantics, not PUT. This handler was written when every caller sent
    # the whole form, and then partial callers appeared: the settings page saves
    # the documentation language on its own as {"docs_language": "de"}, and
    # `payload.temperature` would then be the Pydantic DEFAULT rather than the
    # workspace's value — so choosing a language silently reset the provider,
    # the model, the temperature and the token limit.
    #
    # `model_fields_set` is the only way to tell "not sent" from "sent as the
    # default value"; `or prev.get(...)` cannot, because 0.0 and "" are
    # legitimate settings that are also falsy.
    sent = payload.model_fields_set

    def _keep(field: str, fallback=None):
        """The submitted value if this request carried the field, else what the
        workspace already had."""
        if field in sent:
            return getattr(payload, field)
        return prev.get(field, fallback)

    cfg = {
        "provider": _keep("provider"),
        "model": _keep("model"),
        "temperature": _keep("temperature", 0.1),
        "max_output_tokens": _keep("max_output_tokens", 4096),
        "system_prompt_extras": _keep("system_prompt_extras", ""),
        "openrouter_enabled": _keep("openrouter_enabled", False),
        "connection_last_verified": prev.get("connection_last_verified"),
        "profiles": dict(prev.get("profiles") or {}),
        "agents": {
            name: dict(entry)
            for name, entry in (prev.get("agents") or {}).items()
            if isinstance(entry, dict)
        },
        "embeddings_indexed_signature": prev.get("embeddings_indexed_signature"),
        "review_engine": payload.review_engine or prev.get("review_engine") or "api",
        "review_language": payload.review_language or prev.get("review_language") or "en",
        # Through `_keep`, not `or prev.get(...)`: "" is a legitimate value
        # here — it is how the form says "no fallback any more" — and the
        # falsy-fallthrough would silently resurrect the cleared model.
        "review_fallback_model": _keep("review_fallback_model"),
        "docs_language": resolve_docs_language(
            payload.docs_language or prev.get("docs_language")),
        "docs_engine": payload.docs_engine or prev.get("docs_engine") or "api",
    }
    # Merge per-surface profile updates.
    if payload.profiles:
        from src.llm.profiles import PROFILE_NAMES
        for name, entry in payload.profiles.items():
            if name not in PROFILE_NAMES or not isinstance(entry, dict):
                continue
            cur = dict(cfg["profiles"].get(name) or {})
            if entry.get("provider"):
                cur["provider"] = entry["provider"]
            if entry.get("model"):
                cur["model"] = entry["model"]
            if name == "embeddings" and entry.get("dimensions"):
                cur["dimensions"] = int(entry["dimensions"])
            if name == "embeddings" and entry.get("provider") and \
                    entry["provider"] not in (*_EMBEDDINGS_PROVIDERS, _LOCAL_PROVIDER):
                # Without this guard the save succeeds and the failure
                # surfaces at index time, inside a queued job with nobody
                # watching (litellm has no embeddings route for these
                # vendors). See _EMBEDDINGS_PROVIDERS.
                raise HTTPException(status_code=422, detail=(
                    f"provider '{entry['provider']}' has no embeddings API "
                    "LiteLLM can route to — embeddings can use google, openai "
                    "or mistral. For a self-hosted embedder, set the "
                    "EMBEDDING_* env variables — see "
                    "GET /api/llm/local-setup-guide."
                ))
            if "base_url" in entry:
                if name not in _BASE_URL_SURFACES:
                    raise HTTPException(status_code=422, detail=(
                        "base_url applies to the chat, review and agent "
                        "surfaces only. Embeddings are configured at the "
                        "installation level via EMBEDDING_* env variables — "
                        "see GET /api/llm/local-setup-guide."
                    ))
                cur["base_url"] = _validated_base_url(entry["base_url"])
            if cur.get("provider") == _LOCAL_PROVIDER:
                if name not in _BASE_URL_SURFACES:
                    raise HTTPException(status_code=422, detail=(
                        f"provider '{_LOCAL_PROVIDER}' is available for the "
                        "chat, review and agent surfaces only. For "
                        "embeddings, set the EMBEDDING_* env variables — see "
                        "GET /api/llm/local-setup-guide."
                    ))
                if not cur.get("base_url"):
                    # Fail closed: a self-hosted profile without an address is
                    # not "temporarily incomplete". Its LiteLLM model string is
                    # "openai/<model>", so the first call made without an
                    # api_base goes to api.openai.com — the one place a
                    # self-hosted profile exists to avoid.
                    raise HTTPException(status_code=422, detail=(
                        f"provider '{_LOCAL_PROVIDER}' requires a base_url "
                        "(e.g. http://127.0.0.1:11434/v1)"
                    ))
            elif "base_url" in cur:
                if "base_url" in entry:
                    raise HTTPException(status_code=422, detail=(
                        f"base_url is only meaningful with provider "
                        f"'{_LOCAL_PROVIDER}'"
                    ))
                # Provider moved back to a hosted one: the address belongs to
                # the self-hosted selection, and a stale api_base would
                # silently redirect the hosted provider's calls.
                cur.pop("base_url", None)
            cfg["profiles"][name] = cur
        # Mirror review profile to legacy top-level fields for back-compat.
        if "review" in payload.profiles:
            rv = cfg["profiles"]["review"]
            cfg["provider"] = rv.get("provider", payload.provider)
            cfg["model"] = rv.get("model", payload.model)
    # After the profiles merge on purpose: the fallback is judged against the
    # primary this save ends up with — whichever of the pair this request
    # changed (see _validated_review_fallback).
    cfg["review_fallback_model"] = _validated_review_fallback(cfg, workspace_id)
    # Per-agent overrides last, so they are validated against the profile this
    # same request is saving rather than the one it replaced.
    if payload.agents is not None:
        cfg["agents"] = _agent_overrides_from_payload(
            payload.agents, cfg, workspace_id,
        )
    _save_workspace_config(cfg, updated_by=user.email, workspace_id=workspace_id)
    # Config changed → drop cached Gemini clients so it takes effect at once.
    try:
        from src.llm.completion import reset_caches
        reset_caches()
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "llm_config_saved provider=%s model=%s temp=%s workspace=%s user=%s",
        payload.provider, payload.model, payload.temperature, workspace_id, user.email,
    )
    # A git connection being saved was audited. The LLM keys — which every
    # model call in this workspace is billed to, and which can be pointed at a
    # different provider entirely — were not.
    #
    # `detail` carries the SHAPE: which providers, which slot, whether the
    # routing changed. Never a key, never a prefix of one, not even a length.
    if keys_saved:
        record_action(
            action="llm_key.saved", actor=user.email, actor_id=user.id,
            workspace_id=workspace_id, target=",".join(sorted(set(keys_saved)))[:200],
            ip=client_ip(request),
            detail={"providers": sorted(set(keys_saved)), "slot": slot},
        )
    # The routing is a separate act from the key, and the one with the larger
    # blast radius: re-pointing the review surface sends every diff in the
    # workspace somewhere new, and needs no key saved at all. An audit that
    # only fired on `keys_saved` would miss it entirely.
    #
    # PATCH semantics (see above) make "what changed" the only honest
    # question — an absent field means untouched, not cleared — so the row
    # names the fields this request actually carried.
    routing = {
        "provider": payload.provider, "model": payload.model,
        "profiles": payload.profiles, "review_engine": payload.review_engine,
        "docs_engine": payload.docs_engine,
    }
    routing = {k: v for k, v in routing.items() if v}
    if routing:
        record_action(
            action="llm_config.changed", actor=user.email, actor_id=user.id,
            workspace_id=workspace_id,
            target=str(payload.provider or payload.review_engine or "routing")[:200],
            ip=client_ip(request),
            detail={k: str(v)[:300] for k, v in routing.items()},
        )
    return get_config(user=user, workspace_id=workspace_id)


@router.post("/test-connection", response_model=TestConnectionOut)
def test_connection(
    payload: TestConnectionIn,
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> TestConnectionOut:
    """Cheap ping: hit the provider's /models endpoint (all providers) or
    /key endpoint (OpenRouter, which surfaces the balance).

    Never spends more than ~$0.0001 — no completion is made unless a specific
    model is passed and even then the max_tokens is capped at 1.
    """
    import time

    import httpx

    provider = payload.provider
    t0 = time.time()

    # Self-hosted servers take a different probe entirely: the address comes
    # from the payload rather than a vendor table, and the request goes
    # through the egress-checked client — the same transport the indexing
    # path uses, so "the test was green" and "production can reach it" are
    # the same statement.
    if provider == _LOCAL_PROVIDER:
        return _test_local_connection(payload, user=user, workspace_id=workspace_id)

    api_key = payload.api_key
    if not api_key:
        # Only the self-hosted provider above is keyless; the schema keeps
        # api_key optional for its sake, so the hosted branches enforce it.
        return TestConnectionOut(
            ok=False, provider=provider,
            detail="api_key is required for this provider",
        )

    # The UI sends the literal "use-saved" when the input is empty and a key
    # is already stored — resolve the workspace's saved key instead of pinging
    # the provider with the placeholder string (guaranteed 400 otherwise).
    if api_key == "use-saved":
        from src.llm.profiles import get_provider_key
        api_key = get_provider_key(provider, workspace_id)
        if not api_key:
            return TestConnectionOut(
                ok=False, provider=provider,
                detail="No saved key for this provider — paste one first.",
            )

    try:
        endpoint, headers = _verify_url_for(provider, api_key)
    except ValueError as exc:
        return TestConnectionOut(
            ok=False, provider=provider, detail=str(exc),
        )

    try:
        with _provider_ping_client(endpoint, timeout=10.0) as client:
            resp = client.get(endpoint, headers=headers)
        latency_ms = int((time.time() - t0) * 1000)
    except httpx.HTTPError as exc:
        return TestConnectionOut(
            ok=False, provider=provider, detail=f"network error: {exc}",
        )

    if resp.status_code == 200:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}

        # OpenRouter's /key endpoint gives us the balance.
        balance = None
        if provider == "openrouter":
            balance = body.get("data", {}).get("limit_remaining")
            if balance is not None:
                try:
                    balance = float(balance)
                except (TypeError, ValueError):
                    balance = None

        # Everyone else's /models returns a `data: [...]` array.
        models_count: int | None = None
        if isinstance(body, dict):
            data = body.get("data") or body.get("models")
            if isinstance(data, list):
                models_count = len(data)

        # Record verification timestamp on the workspace config row.
        _record_verified(user_email=user.email, provider=provider, workspace_id=workspace_id)

        return TestConnectionOut(
            ok=True, provider=provider,
            detail=f"connected — {provider}",
            latency_ms=latency_ms,
            models_available=models_count,
            balance_usd=balance,
        )

    if resp.status_code in (401, 403):
        return TestConnectionOut(
            ok=False, provider=provider,
            detail=f"invalid API key ({resp.status_code})",
            latency_ms=latency_ms,
        )
    return TestConnectionOut(
        ok=False, provider=provider,
        detail=f"provider returned {resp.status_code}",
        latency_ms=latency_ms,
    )


# ─── Self-hosted (OpenAI-compatible) probes ──────────────────────────


def _test_local_connection(
    payload: TestConnectionIn, *, user: User, workspace_id: str,
) -> TestConnectionOut:
    """Probe a self-hosted OpenAI-compatible server.

    Goes through :func:`src.security.egress.build_http_client` — the same
    whitelist transport the embeddings seam uses — so the probe can only reach
    what production calls could reach. A blocked probe is therefore an ANSWER
    (fix the egress config), not a false alarm to route around.
    """
    import time
    from urllib.parse import urlsplit

    import httpx

    from src.config import get_settings
    from src.security.egress import EgressBlockedError, build_http_client

    raw = (payload.base_url or "").strip()
    if not raw:
        return TestConnectionOut(
            ok=False, provider=_LOCAL_PROVIDER,
            detail="base_url is required for the self-hosted provider "
                   "(e.g. http://127.0.0.1:11434/v1)",
        )
    if not raw.startswith(("http://", "https://")):
        return TestConnectionOut(
            ok=False, provider=_LOCAL_PROVIDER,
            detail="base_url must start with http:// or https://",
        )
    base = raw.rstrip("/")
    # Host only in logs: a base_url may carry userinfo
    # (http://user:pass@host:port) and must never reach the log as pasted.
    host = urlsplit(base).hostname or ""
    logger.info("local_probe surface=%s host=%s workspace=%s",
                payload.surface, host, workspace_id)

    api_key = payload.api_key
    if api_key == "use-saved":
        # The UI's "a key is saved, reuse it" placeholder — the same protocol
        # the hosted branch in test_connection speaks. This branch returns
        # BEFORE that resolution, so without resolving here the literal string
        # "use-saved" travelled as the Bearer token and an authenticated local
        # server (vLLM --api-key) answered 401 to a test that should pass.
        from src.llm.profiles import get_provider_key
        api_key = get_provider_key(_LOCAL_PROVIDER, workspace_id)

    headers: dict[str, str] = {}
    if api_key and api_key != _LOCAL_SENTINEL_KEY:
        # A real token (vLLM --api-key, TEI). The sentinel is plumbing, not a
        # credential — our own probes never present it as one.
        headers["Authorization"] = f"Bearer {api_key}"

    settings = get_settings()
    t0 = time.time()
    try:
        with build_http_client(
            settings.egress_allowed_hosts, timeout=10.0,
            allow_private_network=settings.egress_allow_private_network,
        ) as client:
            if payload.surface == "embeddings":
                result = _probe_local_embeddings(client, base, payload.model, headers, t0)
            else:
                result = _probe_local_chat(client, base, payload.model, headers, t0)
    except EgressBlockedError:
        return TestConnectionOut(
            ok=False, provider=_LOCAL_PROVIDER,
            detail=(
                f"egress to '{host}' is blocked: the host is not on the "
                "public allowlist, and private-network destinations are "
                "refused by default. If this server really is on your own "
                "network (loopback / RFC1918), set "
                "EGRESS_ALLOW_PRIVATE_NETWORK=1 — an operator-level switch, "
                "off by default because a client that silently trusts "
                "private addresses is an SSRF hole (the cloud metadata "
                "endpoint is a 'private' address too)."
            ),
        )
    except httpx.HTTPError as exc:
        return TestConnectionOut(
            ok=False, provider=_LOCAL_PROVIDER, detail=f"network error: {exc}",
        )
    if result.ok:
        # set_default_provider=False: the top-level `provider` field mirrors
        # the review profile, and the first-time convenience would fabricate
        # an openai_compatible review profile with no base_url out of a mere
        # connection test — exactly the half-configured state that put_config
        # refuses to save.
        _record_verified(user_email=user.email, provider=_LOCAL_PROVIDER,
                         workspace_id=workspace_id, set_default_provider=False)
    return result


def _probe_local_chat(
    client: Any, base: str, model: str | None,
    headers: dict[str, str], t0: float,
) -> TestConnectionOut:
    """GET {base}/models, falling back to a 1-token completion when absent."""
    import time

    resp = client.get(f"{base}/models", headers=headers)
    latency_ms = int((time.time() - t0) * 1000)
    if resp.status_code == 200:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON 200 is still "reachable"
            body = {}
        data = (body.get("data") or body.get("models") or []) if isinstance(body, dict) else []
        ids = [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]
        detail = (
            f"connected — models: {', '.join(ids[:10])}" if ids
            else "connected — the server lists no models (pull/load one first)"
        )
        return TestConnectionOut(
            ok=True, provider=_LOCAL_PROVIDER, detail=detail,
            latency_ms=latency_ms, models_available=len(ids),
        )
    if resp.status_code in (404, 405):
        # Some servers (bare llama-server builds, single-model shims) skip
        # /models — a 1-token completion answers the same question.
        if not model:
            return TestConnectionOut(
                ok=False, provider=_LOCAL_PROVIDER, latency_ms=latency_ms,
                detail="the server has no /models endpoint — pass a model "
                       "name so the probe can try a 1-token completion",
            )
        resp = client.post(f"{base}/chat/completions", headers=headers, json={
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        })
        latency_ms = int((time.time() - t0) * 1000)
        if resp.status_code == 200:
            return TestConnectionOut(
                ok=True, provider=_LOCAL_PROVIDER, latency_ms=latency_ms,
                detail=f"connected — 1-token completion from '{model}' ok",
            )
        return TestConnectionOut(
            ok=False, provider=_LOCAL_PROVIDER, latency_ms=latency_ms,
            detail=f"server returned {resp.status_code} for a 1-token completion",
        )
    if resp.status_code in (401, 403):
        return TestConnectionOut(
            ok=False, provider=_LOCAL_PROVIDER, latency_ms=latency_ms,
            detail=f"server requires a token ({resp.status_code}) — paste it "
                   "as the API key",
        )
    return TestConnectionOut(
        ok=False, provider=_LOCAL_PROVIDER, latency_ms=latency_ms,
        detail=f"server returned {resp.status_code}",
    )


def _probe_local_embeddings(
    client: Any, base: str, model: str | None,
    headers: dict[str, str], t0: float,
) -> TestConnectionOut:
    """POST {base}/embeddings and report the VECTOR WIDTH.

    The width is the number the operator must see BEFORE indexing: Qdrant
    bakes it into the collection, a mismatched upsert is rejected, and the
    index run that "went green" leaves an empty index behind.
    """
    import time

    body: dict[str, Any] = {"input": ["celmis connection probe"]}
    if model:
        body["model"] = model
    resp = client.post(f"{base}/embeddings", headers=headers, json=body)
    latency_ms = int((time.time() - t0) * 1000)
    if resp.status_code != 200:
        if resp.status_code in (401, 403):
            detail = (f"server requires a token ({resp.status_code}) — paste "
                      "it as the API key")
        else:
            detail = f"server returned {resp.status_code} for /embeddings"
            if not model:
                detail += " — most servers need a model name; pass one"
        return TestConnectionOut(ok=False, provider=_LOCAL_PROVIDER,
                                 detail=detail, latency_ms=latency_ms)
    try:
        vector = (resp.json().get("data") or [{}])[0].get("embedding") or []
    except Exception:  # noqa: BLE001 — shape surprise = not an embeddings endpoint
        vector = []
    width = len(vector)
    if not width:
        return TestConnectionOut(
            ok=False, provider=_LOCAL_PROVIDER, latency_ms=latency_ms,
            detail="the response carried no embedding vector — is this an "
                   "embeddings model?",
        )
    detail = f"connected — embedding width {width}"
    known = _known_collection_width()
    warning: str | None = None
    if known is not None and known != width:
        warning = (
            f"the existing vector collection is {known}-wide — indexing with "
            "this model requires a full re-index (the collection is rebuilt "
            "for the new width)"
        )
    return TestConnectionOut(
        ok=True, provider=_LOCAL_PROVIDER, detail=detail, latency_ms=latency_ms,
        vector_width=width, warning=warning,
    )


def _known_collection_width() -> int | None:
    """Vector width of the existing Qdrant collection, or None when unknown.

    The same lookup reindex_embeddings does before deciding whether the
    collection must be rebuilt. None (no collection yet, Qdrant unreachable)
    is not an error for the probe — it just cannot compare, and says nothing
    rather than guessing.
    """
    try:
        from src.config import get_settings
        from src.retrieval.vector_store import get_vector_client

        settings = get_settings()
        qc = get_vector_client()
        if not qc.collection_exists(settings.qdrant_collection):
            return None
        info = qc.get_collection(settings.qdrant_collection)
        size = getattr(info.config.params.vectors, "size", None)
        return int(size) if size is not None else None
    except Exception as exc:  # noqa: BLE001 — best-effort; the probe's own answer stands
        logger.debug("collection_width_lookup_failed err=%s", exc)
        return None


# ─── Self-hosted setup guide ─────────────────────────────────────────


class LocalSetupGuideOption(BaseModel):
    name: str
    command: str            # paste-into-a-terminal start command
    base_url_hint: str      # the base_url that server answers on, seen from compose
    notes: str


class LocalSetupGuideOut(BaseModel):
    options: list[LocalSetupGuideOption]
    env: list[str]          # the .env lines that pin embeddings to a local server
    reindex_warning: str


@router.get("/local-setup-guide", response_model=LocalSetupGuideOut)
def local_setup_guide(
    user: User = Depends(get_current_user),
) -> LocalSetupGuideOut:
    """Static instructions for pointing a surface at a self-hosted server.

    Authored HERE, in English, on purpose: the commands change when server
    projects change (a new flag, a renamed binary), and the backend ships far
    more often than a translated frontend string catalog. The web renders this
    verbatim (see web/lib/api.ts LocalSetupGuide).
    """
    return LocalSetupGuideOut(
        options=[
            LocalSetupGuideOption(
                name="Ollama",
                command="ollama pull llama3.1:8b && ollama serve",
                base_url_hint="http://host.docker.internal:11434/v1",
                notes=(
                    "Keyless by default — leave the API key empty. Use the "
                    "exact model tag `ollama list` prints as the model name."
                ),
            ),
            LocalSetupGuideOption(
                name="vLLM",
                command="vllm serve Qwen/Qwen2.5-7B-Instruct --port 8000",
                base_url_hint="http://host.docker.internal:8000/v1",
                notes=(
                    "Started with --api-key, the server requires a token — "
                    "paste that token as the API key here."
                ),
            ),
            LocalSetupGuideOption(
                name="llama.cpp",
                command="llama-server -m ./model.gguf --port 8080",
                base_url_hint="http://host.docker.internal:8080/v1",
                notes=(
                    "Serves one model; older builds have no /models endpoint, "
                    "so type the model name yourself before testing."
                ),
            ),
        ],
        # Embeddings are pinned at the installation level ON PURPOSE — indexing
        # ships source code to the embedder, so where it goes is an operator
        # (env) decision, not a dropdown. These are the .env lines; the full
        # commented recipe lives in .env.example ("Local embeddings").
        env=[
            "EMBEDDING_PROVIDER=openai_compatible",
            "EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1",
            "EMBEDDING_MODEL=nomic-embed-text",
            "EMBEDDING_DIMENSIONS=768",
            "EGRESS_ALLOW_PRIVATE_NETWORK=1",
        ],
        reindex_warning=(
            "Switching the embedding model (or its vector width) requires a "
            "full re-index — vectors from two models cannot share one "
            "collection."
        ),
    )


class ProviderModelsOut(BaseModel):
    provider: str
    generation: list[str]
    embedding: list[str]
    detail: str = ""


@router.get("/models", response_model=ProviderModelsOut)
def provider_models(
    provider: str, user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> ProviderModelsOut:
    """List models available for a provider's saved key, split into
    generation- vs embedding-capable, to populate the /settings/llm dropdowns."""
    import httpx

    from src.llm.profiles import get_provider_key
    if provider == _LOCAL_PROVIDER:
        # No vendor endpoint to ask — the server's own /models answers, and
        # the probe needs the base_url which this endpoint does not carry.
        return ProviderModelsOut(
            provider=provider, generation=[], embedding=[],
            detail="self-hosted provider — use POST /api/llm/test-connection "
                   "with the base_url; the probe reports the server's models",
        )
    key = get_provider_key(provider, workspace_id)
    if not key:
        return ProviderModelsOut(provider=provider, generation=[], embedding=[],
                                 detail="no key configured for this provider")
    try:
        endpoint, headers = _verify_url_for(provider, key)
    except ValueError as exc:
        return ProviderModelsOut(provider=provider, generation=[], embedding=[],
                                 detail=str(exc))
    try:
        with _provider_ping_client(endpoint, timeout=12.0) as client:
            resp = client.get(endpoint, headers=headers)
    except httpx.HTTPError as exc:
        return ProviderModelsOut(provider=provider, generation=[], embedding=[],
                                 detail=f"network error: {exc}")
    if resp.status_code != 200:
        return ProviderModelsOut(provider=provider, generation=[], embedding=[],
                                 detail=f"provider returned {resp.status_code}")
    body = resp.json()
    gen: list[str] = []
    emb: list[str] = []
    if provider in ("google", "gemini"):
        for m in (body.get("models") or []):
            name = str(m.get("name", "")).replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            if "generateContent" in methods:
                gen.append(name)
            if any("embed" in x.lower() for x in methods):
                emb.append(name)
    else:
        # OpenAI-compatible /models: {data: [{id: ...}]}
        for m in (body.get("data") or body.get("models") or []):
            mid = m.get("id") if isinstance(m, dict) else str(m)
            if not mid:
                continue
            (emb if "embed" in mid.lower() else gen).append(mid)
    return ProviderModelsOut(
        provider=provider, generation=sorted(set(gen)), embedding=sorted(set(emb)),
    )


class ReindexOut(BaseModel):
    enqueued: int
    repos: list[str]
    signature: str
    detail: str


def _is_single_tenant() -> bool:
    """Whether this installation has anybody else to lose.

    Delegates to src/deployment.py, which is the declared answer. This used
    to COUNT the workspaces table — a second, independently-derived answer to
    a question that now has an authoritative one, and two answers to one
    question is how they start to disagree. The count survives only as the
    fallback for an installation that has not declared a mode.
    """
    # BOTH, and the conjunction is the point. Delegating to the declared mode
    # alone was a regression I introduced and my own test caught: the default
    # mode is single_tenant, so a workspace that has THREE tenants and has
    # simply never declared otherwise would have been told it was safe to
    # drop the shared collection.
    #
    # The declaration says what the operator intends; the count says what is
    # actually in there. Wiping is allowed only when they agree, and an
    # unknown count refuses.
    try:
        from src.deployment import count_workspaces, is_single_tenant

        if not is_single_tenant():
            return False
        n = count_workspaces()
        return n is not None and n <= 1
    except Exception:  # noqa: BLE001 — unknown means "assume shared", refuse
        return False


@router.post("/embeddings/reindex", response_model=ReindexOut)
def reindex_embeddings(
    # Rebuilding the vectors is hours of embedding spend and takes search
    # down while it runs. It was reachable by any member of any workspace.
    user: User = Depends(require_workspace_admin),
) -> ReindexOut:
    """Re-embed every indexed repo with the current `embeddings` profile and
    clear the 'reindex needed' flag. Enqueues one durable job per repo so a
    provider/model switch is applied without blocking the request."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin scope required")
    from src.llm.profiles import embeddings_signature
    from src.sync.queue import KIND_REINDEX_QDRANT, enqueue

    # Discover indexed repos (those with a tree-sitter graph).
    repos: list[str] = []
    try:
        from src.mcp_server import tools as legacy
        repos = [r.slug for r in legacy.list_repos()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("reindex_repo_discovery_failed err=%s", exc)

    # A different embeddings provider/model usually means a different vector
    # width. Qdrant rejects upserts that don't match the collection, so the
    # collection has to be rebuilt BEFORE the re-embed jobs run.
    recreated = False
    try:

        from src.config import get_settings
        from src.llm.completion import embedding_dimensions

        settings = get_settings()
        want = embedding_dimensions()
        from src.retrieval.vector_store import get_vector_client
        qc = get_vector_client()
        coll = settings.qdrant_collection
        if qc.collection_exists(coll):
            info = qc.get_collection(coll)
            cfg = info.config.params.vectors
            current = getattr(cfg, "size", None)
            if current is not None and int(current) != int(want):
                logger.warning(
                    "qdrant_dim_change coll=%s %s -> %s — recreating",
                    coll, current, want,
                )
                # REFUSED unless this installation has one tenant. The
                # collection is SHARED: dropping it to change the vector
                # width wipes every workspace's vectors, and the person
                # pressing the button is changing their own embedding model,
                # not consenting to that. A single-tenant install has nobody
                # else to lose, which is the only case where "recreate" is
                # the same size as the decision being made.
                if not _is_single_tenant():
                    logger.error(
                        "qdrant_dim_change_refused coll=%s %s -> %s — the "
                        "collection is shared across workspaces", coll,
                        current, want,
                    )
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Changing the embedding width would recreate the "
                            "shared vector collection and delete every "
                            "workspace's vectors. Re-index instead, or run "
                            "this on a single-workspace installation."
                        ),
                    )
                qc.delete_collection(coll)
                from qdrant_client import models as qmodels
                qc.create_collection(
                    collection_name=coll,
                    vectors_config=qmodels.VectorParams(
                        size=want, distance=qmodels.Distance.COSINE,
                    ),
                )
                recreated = True
    except HTTPException:
        # The refusal above is a DECISION, not a failure to check. It sat
        # inside this handler, was logged as a warning, and the reindex went
        # on to drop the shared collection anyway — a guard that produces a
        # log line and no consequence.
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("reindex_collection_check_failed err=%s", exc)

    enq = 0
    for slug in repos:
        jid = enqueue(kind=KIND_REINDEX_QDRANT, payload={"repo_slug": slug},
                      dedup_key=f"reindex:{slug}", enqueued_by=user.email)
        if jid:
            enq += 1

    sig = embeddings_signature()
    cfg = _load_workspace_config()
    cfg["embeddings_indexed_signature"] = sig
    _save_workspace_config(cfg, updated_by=user.email)
    detail = f"queued {enq} repo re-embed job(s) with the current embeddings profile"
    if recreated:
        detail += " (vector collection recreated for the new embedding width)"
    return ReindexOut(enqueued=enq, repos=repos, signature=sig, detail=detail)


# ─── Provider verify URL map ────────────────────────────────────────


def _provider_ping_client(endpoint: str, *, timeout: float):
    """Guarded client for one hosted-provider ping.

    `endpoint` is one of the constants in _verify_url_for — never request
    data — so its host may extend the allowlist: api.openai.com,
    api.anthropic.com, openrouter.ai, api.groq.com and api.mistral.ai are not
    on the shipped public list, and without the exception the key ping would
    be refused by the very transport that now guards it.
    """
    from urllib.parse import urlsplit

    from src.http import build_client

    host = urlsplit(endpoint).hostname or ""
    return build_client(timeout=timeout, extra_allowed_hosts=(host,) if host else ())


def _verify_url_for(provider: str, token: str) -> tuple[str, dict[str, str]]:
    if provider == "openai":
        return "https://api.openai.com/v1/models", {
            "Authorization": f"Bearer {token}",
        }
    if provider == "anthropic":
        return "https://api.anthropic.com/v1/models", {
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
        }
    if provider in ("google", "gemini"):
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models?key={token}",
            {},
        )
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1/key", {
            "Authorization": f"Bearer {token}",
        }
    if provider == "groq":
        return "https://api.groq.com/openai/v1/models", {
            "Authorization": f"Bearer {token}",
        }
    if provider == "mistral":
        return "https://api.mistral.ai/v1/models", {
            "Authorization": f"Bearer {token}",
        }
    raise ValueError(f"unknown provider {provider!r}")


def _record_verified(*, user_email: str, provider: str, workspace_id: str = "default",
                     set_default_provider: bool = True) -> None:
    """Bump the `connection_last_verified` timestamp so the UI can show
    'verified 5 min ago' instead of a bare 'connected' badge.

    `set_default_provider=False` for the self-hosted provider: the top-level
    `provider` field mirrors the review profile, and the first-time
    convenience would turn a mere connection test into an openai_compatible
    review profile with no base_url — the state put_config refuses to save.
    """
    from datetime import datetime

    cfg = _load_workspace_config(workspace_id)
    cfg["connection_last_verified"] = datetime.now(UTC).isoformat()
    if set_default_provider and not cfg.get("provider"):
        cfg["provider"] = provider  # first-time convenience
    _save_workspace_config(cfg, updated_by=user_email, workspace_id=workspace_id)


__all__ = ["router"]
