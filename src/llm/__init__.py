"""LLM layer — dispatch, profiles, gateway, and the one native Gemini surface.

The re-exports are lazy (PEP 562) ON PURPOSE. `gemini_client` imports
`google.genai` at module scope, so an eager re-export here made importing ANY
`src.llm` submodule pull the Google SDK in — including `src.llm.gateway`,
whose `EmbeddingConfigMismatch` the local embedder raises. An air-gapped
install has no `google-genai` on disk at all (see
tests/indexing/test_embedder_local.py), and it broke on import — before the
question of sending anything anywhere could even come up.
"""

from __future__ import annotations

from typing import Any

__all__ = ["GeminiClient", "get_gemini_client"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from src.llm import gemini_client

        return getattr(gemini_client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
