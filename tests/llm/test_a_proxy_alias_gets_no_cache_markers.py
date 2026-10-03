"""A workspace LiteLLM proxy alias is not sent `cache_control` breakpoints
unless the proxy says the alias runs on Claude.

The first live proxy (Gemini free tier behind it) answered every review agent
and the Celmis agent with 429 "TotalCachedContentStorageTokensPerModelFreeTier
... limit=0": the markers had been forwarded to Gemini as a context-cache
request. Direct providers and the installation gateway keep the markers.
"""

from __future__ import annotations

import pytest

from src.llm import client as llm_client
from src.llm import litellm_proxy


def _has_markers(messages: list[dict]) -> bool:
    return any(
        "cache_control" in part
        for m in messages
        for part in (m["content"] if isinstance(m["content"], list) else [])
    )


def _messages(model: str, ws: str = "ws-1") -> list[dict]:
    return llm_client._build_messages(
        prompt="review this",
        system_instruction="you are a reviewer",
        redacted_code="def f(): pass",
        cache_breakpoints=llm_client._cache_breakpoints_welcome(model, ws),
    )


@pytest.fixture
def proxy(monkeypatch):
    """A workspace with a stored proxy whose /model/info maps two aliases."""
    endpoint = object()
    upstream = {"flash": "gemini/gemini-3.5-flash-lite", "sonnet": "anthropic/claude-sonnet-5-5"}
    monkeypatch.setattr(litellm_proxy, "resolve_endpoint",
                        lambda ws, *a, **k: endpoint if ws == "ws-1" else None)
    monkeypatch.setattr(litellm_proxy, "underlying_model",
                        lambda ep, alias: upstream.get(alias) if ep is endpoint else None)


def test_a_gemini_alias_behind_the_proxy_gets_no_markers(proxy):
    assert not _has_markers(_messages("litellm_proxy/flash"))


def test_a_claude_alias_behind_the_proxy_keeps_them(proxy):
    assert _has_markers(_messages("litellm_proxy/sonnet"))


def test_an_alias_the_proxy_cannot_explain_gets_none(proxy):
    assert not _has_markers(_messages("litellm_proxy/mystery"))


def test_the_installation_gateway_is_unchanged(proxy):
    # No workspace proxy stored: the litellm_proxy/ route is the gateway.
    assert _has_markers(_messages("litellm_proxy/celmis-ws-2-review", ws="ws-2"))


def test_direct_providers_are_unchanged(proxy):
    assert _has_markers(_messages("gemini/gemini-3.6-flash"))
    assert _has_markers(_messages("anthropic/claude-sonnet-5-5"))


def test_the_text_is_the_same_either_way(proxy):
    def texts(ms):
        return [p["text"] for m in ms for p in m["content"]]
    assert texts(_messages("litellm_proxy/flash")) == texts(_messages("gemini/gemini-3.6-flash"))
