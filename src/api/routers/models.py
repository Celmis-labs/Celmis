"""Model catalog endpoints (Stage 11).

    GET /api/models/available          — models grouped by provider, filtered
                                         to providers the current user has
                                         a key for. Each entry includes pricing
                                         from the merged LiteLLM + OpenRouter
                                         table so the UI can render
                                         "$3/M in $15/M out" next to the name.

    POST /api/models/refresh-pricing   — force overlay refresh from OpenRouter
                                         (background APScheduler will run this
                                         daily; endpoint is for on-demand ops).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from src.api.deps import current_workspace_id, get_current_user
from src.llm.keys import list_configured_providers
from src.llm.pricing import get_pricing_resolver
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])


# Map LiteLLM's `litellm_provider` label → the provider slug we use in the
# credentials store. LiteLLM's tag set is broader (bedrock, vertex_ai-…) than
# our BYOK enum, so we normalise + filter here.
_PROVIDER_MAP = {
    "openai": "openai",
    "azure": "openai",
    "anthropic": "anthropic",
    "gemini": "google",
    "vertex_ai-language-models": "google",
    "openrouter": "openrouter",
    "groq": "groq",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "together_ai": "together_ai",
}

# Recommended defaults per agent — shown as pre-selected options in the UI.
_RECOMMENDED_TAGS = {
    "anthropic/claude-sonnet-5": "architect+security (default)",
    "anthropic/claude-opus-4-8": "architect+security (premium)",
    "anthropic/claude-haiku-4-5": "quality+tests (Anthropic cheap)",
    "openai/gpt-4o": "architect+security (OpenAI)",
    "openai/gpt-4o-mini": "quality+tests (cheap)",
    "gemini/gemini-3-pro-preview": "architect+security (Google)",
    "gemini/gemini-3-flash-preview": "quality+tests (Google cheap)",
}


@router.get("/available")
def list_available_models(
    *,
    all_providers: bool = Query(
        False,
        description="If true — include models from providers the user has NOT "
                    "connected. Useful for previewing catalog before adding a key.",
    ),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Return a JSON structure the UI can render as a grouped dropdown.

    Shape:
        {
          "connected_providers": ["gitlab", "google"],
          "available_providers": ["openai", "anthropic", ...],
          "models": [
            {
              "id": "anthropic/claude-sonnet-5",
              "provider": "anthropic",
              "input_per_m": 3.0,
              "output_per_m": 15.0,
              "max_context": 200000,
              "recommended_for": "architect+security (default)",
              "available": true      // false if user hasn't connected `anthropic`
            },
            ...
          ]
        }
    """
    configured = set(list_configured_providers(user_id=user.id, workspace_id=workspace_id))
    resolver = get_pricing_resolver()

    models: list[dict[str, Any]] = []
    for name in resolver.known_models():
        price = resolver.get(name)
        if price is None:
            continue

        # Normalise LiteLLM's provider tag → our BYOK slug. Skip models whose
        # provider we don't support (bedrock-only variants, etc).
        provider = _PROVIDER_MAP.get(price.provider)
        if provider is None:
            # Try prefix inference — "anthropic/claude-x" always maps to anthropic.
            if "/" in name and name.split("/", 1)[0] in _PROVIDER_MAP:
                provider = _PROVIDER_MAP[name.split("/", 1)[0]]
            else:
                continue

        available = provider in configured
        if not available and not all_providers:
            continue

        # Filter noise — skip embeddings / moderation / audio / image models.
        low = name.lower()
        if any(kw in low for kw in (
            "embed", "whisper", "tts-", "dall-e", "moderation",
            "vision-preview", "image", "audio-",
        )):
            continue

        models.append({
            "id": name,
            "provider": provider,
            "input_per_m": round(price.input_per_million, 4),
            "output_per_m": round(price.output_per_million, 4),
            "max_context": price.max_input_tokens,
            "recommended_for": _RECOMMENDED_TAGS.get(name),
            "available": available,
        })

    # Sort so recommended models come first, then by (provider, cheapest input).
    models.sort(key=lambda m: (
        m["recommended_for"] is None,     # False < True — recommended first
        m["provider"],
        m["input_per_m"],
    ))

    return {
        "connected_providers": sorted(configured),
        "available_providers": sorted(_PROVIDER_MAP.values()),
        "models": models,
        "pricing_last_refreshed": (
            resolver._last_overlay_refresh.isoformat()  # noqa: SLF001
            if resolver._last_overlay_refresh else None
        ),
    }


@router.post("/refresh-pricing")
def refresh_pricing(
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Force an OpenRouter pricing overlay refresh. Any authenticated user
    can trigger; the underlying HTTP call is cheap."""
    resolver = get_pricing_resolver()
    added = resolver.refresh_overlay()
    return {
        "overlay_entries": added,
        "last_refreshed": (
            resolver._last_overlay_refresh.isoformat()  # noqa: SLF001
            if resolver._last_overlay_refresh else None
        ),
    }
