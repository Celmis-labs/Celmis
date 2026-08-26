"""The two names one Gemini key travels under, and the two readings of one model string.

A workspace configured entirely through the UI stores its Gemini credential
under the provider name "google" — that is what the Connections page writes.
The review path derives its provider from the model string instead, and a bare
"gemini-3.7-flash" reads as "gemini". Nothing reconciled the two, so every
agent failed with "no API key is configured for this workspace" while
/settings/llm showed the key saved and its Test button passed.

The same model string is read a second time, by LiteLLM, which calls a bare
"gemini-*" Vertex AI and goes looking for Application Default Credentials that
no container holds. Fixing only the first reading moved the failure from
"no key" to DefaultCredentialsError — which is why both are asserted here.
"""

from __future__ import annotations

import pytest

from src.llm.keys import LLMCredentialError


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class _Response:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


@pytest.fixture
def gemini_key_saved_as_google(monkeypatch):
    """Exactly what the Connections page leaves behind: a key under "google",
    and nothing under "gemini"."""
    import src.llm.keys as keys

    def _resolve(provider, *, user_id=None, workspace_id="default", **kw):
        if provider == "google":
            return "test-key-from-the-connections-page"
        raise LLMCredentialError(
            f"no {provider!r} key for workspace {workspace_id!r}."
        )

    monkeypatch.setattr(keys, "resolve_api_key", _resolve)
    return _resolve


@pytest.fixture
def captured_litellm(monkeypatch):
    import litellm

    seen: list[dict] = []

    def _fake(**kwargs):
        seen.append(kwargs)
        return _Response("[]")

    monkeypatch.setattr(litellm, "completion", _fake)
    return seen


def test_the_key_is_found_under_the_name_the_ui_wrote(
    gemini_key_saved_as_google, captured_litellm,
):
    from src.llm.client import build_llm_client

    client = build_llm_client("system", "ws-under-test")
    client.generate(
        prompt="p", model="gemini-3.7-flash", agent="quality", operation="review_quality",
    )

    assert captured_litellm, "the provider was never reached"
    assert captured_litellm[0]["api_key"] == "test-key-from-the-connections-page"


def test_a_bare_gemini_model_is_not_handed_to_litellm_as_vertex(
    gemini_key_saved_as_google, captured_litellm,
):
    from src.llm.client import build_llm_client

    client = build_llm_client("system", "ws-under-test")
    client.generate(
        prompt="p", model="gemini-3.7-flash", agent="quality", operation="review_quality",
    )

    model = captured_litellm[0]["model"]
    assert model == "gemini/gemini-3.7-flash", (
        f"LiteLLM was handed {model!r}; bare 'gemini-*' routes to Vertex AI and "
        f"fails on Application Default Credentials"
    )


def test_a_provider_with_no_alias_still_fails_loudly(monkeypatch, captured_litellm):
    """The fallback must not turn every missing key into a google lookup."""
    import src.llm.keys as keys
    from src.llm.client import build_llm_client

    def _none(provider, *, user_id=None, workspace_id="default", **kw):
        raise LLMCredentialError(f"no {provider!r} key")

    monkeypatch.setattr(keys, "resolve_api_key", _none)
    client = build_llm_client("system", "ws-under-test")
    with pytest.raises(LLMCredentialError):
        client.generate(
            prompt="p", model="claude-sonnet-5", agent="quality",
            operation="review_quality",
        )
