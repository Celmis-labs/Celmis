"""LLM API key resolver (Stage 11 — BYOK).

Three-tier fallback per provider:

    1. User-scoped     — credentials_v2 row with (user_id, provider, "default")
    2. Default-user    — credentials_v2 row with ("default", provider, "default")
                         (CLI convention — `analyzer review ...` uses this path)
    3. Environment     — ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY /
                         OPENROUTER_API_KEY / GROQ_API_KEY / etc.

Placeholder values from `.env.example` (`replace-me`, `change-me`, empty string)
are blacklisted at env-lookup so a copy-pasted template is treated as "no key".

Errors are always `LLMCredentialError`. The message never contains the secret
itself, only its provenance and a hint to the Connections page.
"""

from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger(__name__)


# ─── Public exception ────────────────────────────────────────────────


class LLMCredentialError(RuntimeError):
    """Provider key missing, corrupted, or explicitly placeholder-blacklisted."""


# Legacy credentials-store `user_id` under which single-tenant provider keys
# lived before per-workspace isolation. Still readable, but ONLY for the
# "default" transition tenant — see `_slot_chain`.
WORKSPACE_KEY_USER: Final[str] = "workspace"

# Per-workspace credential slots are keyed `ws:{workspace_id}`. This is the
# authoritative slot: an isolated tenant resolves ONLY its own row (then env),
# never a sibling's — otherwise an empty new workspace would silently spend on,
# and leak private code through, another tenant's provider account.
WORKSPACE_SLOT_PREFIX: Final[str] = "ws:"


def workspace_slot(workspace_id: str) -> str:
    """The credentials-store `user_id` for a workspace's own keys."""
    return f"{WORKSPACE_SLOT_PREFIX}{workspace_id}"


def _slot_chain(workspace_id: str, user_id: str) -> list[str]:
    """Ordered credential slots to try for this workspace.

    The default/transition tenant keeps reading the legacy shared (`workspace`),
    personal (`user_id`) and CLI (`default`) slots so pre-isolation keys keep
    working. Every other workspace is strictly isolated to its own `ws:{id}`
    slot; env is the only shared fallback (the operator's house key), applied by
    the caller after this chain is exhausted.
    """
    primary = workspace_slot(workspace_id)
    if workspace_id == "default":
        return list(dict.fromkeys((primary, WORKSPACE_KEY_USER, user_id, "default")))
    return [primary]


# ─── Provider registry ───────────────────────────────────────────────

# Provider slug → env-var fallback name. Additions here need one line in the
# Connections UI select and one line in `_KNOWN_PROVIDERS`.
_ENV_FALLBACK: Final[dict[str, str]] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",       # LiteLLM uses "google", we've historically used GEMINI_API_KEY
    "gemini": "GEMINI_API_KEY",       # alias — same underlying key
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "together_ai": "TOGETHER_API_KEY",
    # Self-hosted OpenAI-compatible server (vLLM, llama.cpp, LM Studio, …).
    # Same slug the embeddings side uses in src/config.py. The env var is for
    # servers started WITH auth (vLLM --api-key); a keyless server resolves to
    # the LOCAL_NO_KEY sentinel below instead of failing.
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}

_KNOWN_PROVIDERS: Final[frozenset[str]] = frozenset(_ENV_FALLBACK.keys())

#: What "openai_compatible" resolves to when nobody stored a key. Local
#: servers usually run without auth, but every layer between here and litellm
#: treats an empty key as "not configured" and refuses the call — so the
#: keyless state needs a non-empty value. litellm will pass it as a Bearer
#: token; a keyless local server ignores the header. Our own code must never
#: treat it as a real credential: in particular it must not make a local
#: surface eligible for LiteLLM-gateway provisioning (refused in
#: profiles._attach_gateway and gateway._plan — see those for the WHY).
LOCAL_NO_KEY: Final[str] = "local-no-key"

# Placeholder values that must never be treated as valid credentials.
# These come from `.env.example` — copy-paste boo-boos are common.
_PLACEHOLDER_VALUES: Final[frozenset[str]] = frozenset({
    "", "replace-me", "change-me", "your-key-here",
    "replace-with-strong-random-password",
    "replace-with-openssl-rand-hex-32",
    "your-api-key", "sk-your-key-here",
})


# ─── Resolver ────────────────────────────────────────────────────────


def _is_placeholder(value: str) -> bool:
    stripped = value.strip()
    if stripped in _PLACEHOLDER_VALUES:
        return True
    # Also skip anything under 8 chars — no real API key is that short.
    return len(stripped) < 8


def resolve_api_key(
    provider: str, user_id: str = "default", *, workspace_id: str = "default",
) -> str:
    """Resolve an LLM API key for `provider` scoped to `workspace_id`.

    Never returns None or empty. On failure raises `LLMCredentialError` with a
    message that points the user (or CLI operator) at the Connections page.
    Secret values are guaranteed to be absent from the exception text.

    `workspace_id` selects the authoritative `ws:{id}` slot. Non-default
    workspaces resolve strictly their own slot then env — never another
    tenant's. `user_id` only matters for the legacy "default" transition tenant.
    """
    if provider not in _KNOWN_PROVIDERS:
        raise LLMCredentialError(
            f"unsupported provider {provider!r}; known: "
            f"{sorted(_KNOWN_PROVIDERS)}"
        )

    # ── Tier 1 + 2: credentials store (user-scoped, then default user) ──
    try:
        from src.credentials import get_credential_store
        from src.credentials.store import CredentialStoreError

        store = get_credential_store()
    except Exception as exc:  # noqa: BLE001
        logger.debug("credential_store_unavailable err=%s", exc)
        store = None

    if store is not None:
        # Authoritative slot is `ws:{workspace_id}`. Only the default/transition
        # tenant falls through to the legacy shared/personal/default rows; every
        # other workspace is isolated to its own slot (see `_slot_chain`).
        primary_slot = workspace_slot(workspace_id)
        for lookup_user in _slot_chain(workspace_id, user_id):
            try:
                stored = store.load(
                    provider=provider,
                    user_id=lookup_user,
                    account_label="default",
                )
            except CredentialStoreError as exc:
                # Fernet decryption failure → corrupted row. Surface a clear
                # message so operator knows to re-add on Connections page
                # (or rotate master key).
                logger.warning(
                    "llm_key_decrypt_failed provider=%s user=%s err=%s",
                    provider, lookup_user, exc,
                )
                raise LLMCredentialError(
                    f"credentials corrupted for provider={provider!r}, "
                    f"user={lookup_user!r}. Re-add on the Connections page "
                    f"(or run `analyzer credentials verify`)."
                ) from exc
            if stored is not None and stored.secret:
                if _is_placeholder(stored.secret):
                    # Ignore placeholder — fall through to next tier.
                    logger.debug(
                        "llm_key_placeholder_ignored provider=%s user=%s",
                        provider, lookup_user,
                    )
                    continue
                if lookup_user != primary_slot:
                    logger.warning(
                        "llm_key_legacy_slot provider=%s workspace=%s slot=%s — "
                        "resolved via a legacy shared slot; re-save this key on "
                        "the Connections page to isolate it to this workspace",
                        provider, workspace_id, lookup_user,
                    )
                return stored.secret

    # ── Tier 3: environment fallback ──
    env_name = _ENV_FALLBACK[provider]
    env_value = os.environ.get(env_name, "")
    if env_value and not _is_placeholder(env_value):
        return env_value

    # ── Tier 4 (openai_compatible only): the keyless-local sentinel ──
    # A self-hosted server with no auth has no key to store; refusing here
    # would make "no key" mean "no local LLM", and keyless is the NORMAL
    # state for a local server. A stored token or the env var above still
    # wins (tiers 1-3), so an authenticated vLLM/TEI keeps working.
    if provider == "openai_compatible":
        return LOCAL_NO_KEY

    raise LLMCredentialError(
        f"no {provider!r} key for workspace {workspace_id!r}. "
        f"Add it on the Connections page, or set {env_name} in your .env."
    )


def has_key(
    provider: str, user_id: str = "default", *, workspace_id: str = "default",
) -> bool:
    """Convenience — returns True if `resolve_api_key` would succeed for this
    (provider, workspace). Never raises. Useful for UI ("is provider enabled")."""
    try:
        resolve_api_key(provider, user_id, workspace_id=workspace_id)
        return True
    except LLMCredentialError:
        return False


def list_configured_providers(
    user_id: str = "default", *, workspace_id: str = "default",
) -> list[str]:
    """Return providers that have a resolvable key for this workspace. Sorted."""
    return sorted(
        p for p in _KNOWN_PROVIDERS if has_key(p, user_id, workspace_id=workspace_id)
    )


__all__ = [
    "LLMCredentialError",
    "LOCAL_NO_KEY",
    "resolve_api_key",
    "has_key",
    "list_configured_providers",
    "workspace_slot",
    "WORKSPACE_SLOT_PREFIX",
    "WORKSPACE_KEY_USER",
]
