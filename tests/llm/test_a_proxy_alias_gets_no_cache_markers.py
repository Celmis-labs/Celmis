"""A workspace LiteLLM proxy alias is not sent `cache_control` breakpoints
unless the proxy says the alias runs on Claude.

The first live proxy (Gemini free tier behind it) answered every review agent
and the Celmis agent with 429 "TotalCachedContentStorageTokensPerModelFreeTier
... limit=0": the markers had been forwarded to Gemini as a context-cache
request. Direct providers and the installation gateway keep the markers — and
the decision is read from the ROUTE of the call, so a gateway call in a
workspace that also has a proxy saved keeps them too.
"""

from __future__ import annotations

from src.llm import client as llm_client


def _has_markers(messages: list[dict]) -> bool:
    return any(
        "cache_control" in part
        for m in messages
        for part in (m["content"] if isinstance(m["content"], list) else [])
    )


def _client(*, workspace_proxy: bool, upstream: dict[str, str]):
    """A client whose factory hooks say which route a call takes and what the
    proxy reports behind each alias (as build_llm_client wires them)."""
    def billing(resolved: str) -> str:
        alias = resolved.split("/", 1)[1]
        return upstream.get(alias, alias)

    return llm_client.LLMClient(
        resolve_key=lambda provider: "k",
        resolve_billing_model=billing,
        estimate_proxy_cost=lambda: workspace_proxy,
    )


def _messages(c, model: str) -> list[dict]:
    provider = llm_client._provider_of(model)
    return llm_client._build_messages(
        prompt="review this",
        system_instruction="you are a reviewer",
        redacted_code="def f(): pass",
        cache_breakpoints=c._cache_breakpoints_for(model, provider),
    )


UPSTREAM = {"flash": "gemini/gemini-3.5-flash-lite",
            "sonnet": "anthropic/claude-sonnet-5-5"}


def test_a_gemini_alias_behind_the_workspace_proxy_gets_no_markers():
    c = _client(workspace_proxy=True, upstream=UPSTREAM)
    assert not _has_markers(_messages(c, "litellm_proxy/flash"))


def test_a_claude_alias_behind_the_workspace_proxy_keeps_them():
    c = _client(workspace_proxy=True, upstream=UPSTREAM)
    assert _has_markers(_messages(c, "litellm_proxy/sonnet"))


def test_an_alias_the_proxy_cannot_explain_gets_none():
    c = _client(workspace_proxy=True, upstream=UPSTREAM)
    assert not _has_markers(_messages(c, "litellm_proxy/mystery"))


def test_a_gateway_call_keeps_them_even_with_a_proxy_saved():
    # The route says gateway; whatever the workspace also stores is irrelevant.
    c = _client(workspace_proxy=False, upstream=UPSTREAM)
    assert _has_markers(_messages(c, "litellm_proxy/celmis-ws-2-review"))
    assert _has_markers(_messages(c, "litellm_proxy/flash"))


def test_direct_providers_are_unchanged():
    c = _client(workspace_proxy=True, upstream=UPSTREAM)
    assert _has_markers(_messages(c, "gemini/gemini-3.6-flash"))
    assert _has_markers(_messages(c, "anthropic/claude-sonnet-5-5"))


def test_the_text_is_the_same_either_way():
    c = _client(workspace_proxy=True, upstream=UPSTREAM)

    def texts(ms):
        return [p["text"] for m in ms for p in m["content"]]
    assert texts(_messages(c, "litellm_proxy/flash")) == \
        texts(_messages(c, "gemini/gemini-3.6-flash"))
