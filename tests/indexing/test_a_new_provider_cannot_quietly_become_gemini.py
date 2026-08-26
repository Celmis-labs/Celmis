"""The embedder selector refuses an unimplemented provider instead of guessing.

Two different mistakes are possible with EMBEDDING_PROVIDER, and they are
caught in two different places:

  a typo ("ollama", "openai-compatible") — rejected by
  Settings.known_embedding_provider at construction, so it never reaches the
  selector. Covered here so the two layers stay honest about their jobs.

  a provider added to config.EMBEDDING_PROVIDERS with no branch in
  embedder.get_embedder — passes validation, and used to hit an
  `else: GeminiEmbedder` fallthrough. The operator configures "cohere",
  believes the code goes to Cohere, and it goes to Google. Indexing is the
  one call that ships source code, so the selector refuses.
"""

from __future__ import annotations

import pytest

from src import config
from src.config import Settings
from src.indexing.vectors.embedder import (
    OpenAICompatibleEmbedder,
    get_embedder,
)


def _settings(provider: str = "gemini") -> Settings:
    return Settings(
        embedding_provider=provider,
        embedding_base_url="http://127.0.0.1:9/v1",
        embedding_model="m",
        gemini_api_key="dummy-for-tests",
    )


@pytest.mark.parametrize("typo", ["ollama", "openai-compatible", "local", "vllm"])
def test_a_typo_is_refused_by_config_before_the_selector_sees_it(typo):
    with pytest.raises(ValueError, match="not one of"):
        _settings(typo)


def test_config_forgives_only_shape_not_spelling():
    """A stray space or capital is a copy-paste artefact from a .env line."""
    assert _settings("  GEMINI  ").embedding_provider == "gemini"
    assert _settings("OpenAI_Compatible").embedding_provider == "openai_compatible"


def test_the_one_implemented_provider_resolves():
    assert isinstance(
        get_embedder(_settings("openai_compatible")), OpenAICompatibleEmbedder,
    )


def test_the_valid_value_this_module_does_not_answer_for_also_refuses():
    """"gemini" passes config validation and has no embedder here any more.

    That is not a gap: `completion._configured_embedder()` returns None for it
    and never calls this selector, because the Gemini path needs a workspace
    profile, a key, a gateway route and a ledger row that this seam knows
    nothing about. The refusal has to name that door — the failure it prevents
    is a caller who skipped it and got plausible vectors anyway.
    """
    with pytest.raises(ValueError) as exc:
        get_embedder(_settings("gemini"))
    assert "src.llm.completion.embed" in str(exc.value)


def test_a_provider_allowed_by_config_but_unimplemented_refuses(monkeypatch):
    """The real hazard: someone extends the allowlist and forgets this module."""
    monkeypatch.setattr(
        config, "EMBEDDING_PROVIDERS", ("gemini", "openai_compatible", "cohere"),
    )
    settings = _settings("cohere")
    assert settings.embedding_provider == "cohere", "config now accepts it"

    with pytest.raises(ValueError) as exc:
        get_embedder(settings)
    assert "no implementation" in str(exc.value)
    assert "Gemini" in str(exc.value), (
        "the message must name what the old code would have done, because that "
        "silent fallback is the bug being prevented"
    )
