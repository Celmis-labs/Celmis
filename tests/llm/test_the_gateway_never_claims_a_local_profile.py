"""The LiteLLM gateway must never claim a self-hosted (openai_compatible)
profile — not in the provisioning plan, not at profile resolution.

Why this is an egress bug and not a missing feature: a proxy deployment is
created with litellm_params carrying the model string and the key but NO
api_base (see gateway._upsert_deployment). This provider's model string is
"openai/<model>", so a deployment built from a self-hosted profile would send
the workspace's prompts and code to api.openai.com — the exact vendor the
profile exists to avoid. The "local-no-key" sentinel makes the surface look
provisioning-eligible (it satisfies every non-empty-key check), which is why
the refusal must be explicit and must hold WITH the sentinel present.

Two independent layers are asserted here, in the codebase's usual
belt-and-braces shape:

    * profiles._attach_gateway — never routes a local profile, even when the
      gateway env is fully configured;
    * gateway._plan — never includes a local surface in the provisioning plan.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.llm import gateway
from src.llm.keys import LOCAL_NO_KEY
from src.llm.profiles import Profile, _attach_gateway

BASE_URL = "http://vllm.internal:8000/v1"


def _local(surface: str) -> Profile:
    return Profile(
        surface=surface, provider="openai_compatible", model="qwen3-32b",
        api_key=LOCAL_NO_KEY, api_base=BASE_URL, raw_api_key=LOCAL_NO_KEY,
    )


def _google(surface: str) -> Profile:
    key = "AIzaSy-real-google-key-123"
    return Profile(
        surface=surface, provider="google", model="gemini-3.1-pro-preview",
        api_key=key, raw_api_key=key,
    )


@pytest.fixture(autouse=True)
def gateway_on(monkeypatch):
    """The dangerous configuration: gateway fully enabled. Every assertion in
    this file must hold precisely then — off, the refusals are unreachable."""
    monkeypatch.setenv("LITELLM_PROXY_URL", "http://litellm:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test-key")
    gateway.reset_cache()
    yield
    gateway.reset_cache()
    monkeypatch.delenv("LITELLM_PROXY_API_BASE", raising=False)


@pytest.fixture
def no_proxy_http():
    """Any HTTP to the proxy fails the test — refusal must happen before I/O."""
    with patch.object(
        gateway, "_call",
        side_effect=AssertionError("the proxy was called for a local profile"),
    ) as call:
        yield call


# ─── Layer 1: _attach_gateway ────────────────────────────────────────


def test_attach_gateway_leaves_a_local_profile_direct_even_when_enabled():
    """A cached route for the workspace must not be handed to a local profile:
    the refusal comes before route lookup, so route_for is never consulted."""
    route = MagicMock()
    route.virtual_key = "sk-virtual-1"
    route.deployment = "celmis-ws-a-chat"
    route.base_url = "http://litellm:4000"
    route.underlying_model = "openai/qwen3-32b"

    local = _local("chat")
    with patch.object(gateway, "route_for", return_value=route) as route_for:
        result = _attach_gateway(local, "ws-a")

    assert result == local            # untouched — same fields, no route fields
    assert result.via_gateway is False
    assert result.api_key == LOCAL_NO_KEY
    route_for.assert_not_called()


def test_attach_gateway_still_routes_hosted_profiles():
    """Control: the refusal is scoped to openai_compatible — a hosted profile
    on the same enabled gateway still gets its route."""
    route = MagicMock()
    route.virtual_key = "sk-virtual-2"
    route.deployment = "celmis-ws-a-chat"
    route.base_url = "http://litellm:4000"
    route.underlying_model = "gemini/gemini-3.1-pro-preview"

    with patch.object(gateway, "route_for", return_value=route):
        result = _attach_gateway(_google("chat"), "ws-a")

    assert result.via_gateway is True
    assert result.api_key == "sk-virtual-2"


# ─── Layer 2: the provisioning plan ──────────────────────────────────


def test_the_plan_never_contains_a_local_surface_even_with_the_sentinel_key(monkeypatch):
    """The sentinel satisfies the plan's non-empty-key check by design — the
    exclusion must therefore be by provider, not by key emptiness."""
    monkeypatch.setattr(
        "src.llm.profiles.resolve_profile", lambda surface, ws="default": _local(surface),
    )
    assert gateway._plan("ws-a") == []


def test_the_plan_keeps_hosted_surfaces_and_drops_local_ones(monkeypatch):
    monkeypatch.setattr(
        "src.llm.profiles.resolve_profile",
        lambda surface, ws="default": _google(surface) if surface == "review" else _local(surface),
    )
    entries = gateway._plan("ws-a")
    assert [e.surface for e in entries] == ["review"]
    assert entries[0].model == "gemini/gemini-3.1-pro-preview"


def test_provisioning_an_all_local_workspace_makes_no_proxy_call(
    monkeypatch, no_proxy_http,
):
    monkeypatch.setattr(
        "src.llm.profiles.resolve_profile", lambda surface, ws="default": _local(surface),
    )
    assert gateway.provision_workspace("ws-a") is False
    no_proxy_http.assert_not_called()


def test_ensure_workspace_keys_refuses_the_local_provider(no_proxy_http):
    """The single-provider provisioning entry point refuses outright — even a
    caller that hand-builds the arguments cannot deploy a local profile."""
    result = gateway.ensure_workspace_keys(
        "ws-a", "openai_compatible", LOCAL_NO_KEY, {"chat": "qwen3-32b"},
    )
    assert result is None
    no_proxy_http.assert_not_called()


# ─── End to end: a local chat call stays direct with the gateway on ──


def test_a_local_chat_profile_stays_direct_when_the_gateway_is_on(
    monkeypatch, no_proxy_http,
):
    """`_routed` (the provisioning-on-first-use path every chat call takes)
    must return the local profile as-is: no route attached, no HTTP made."""
    from src.llm import completion

    local = _local("chat")
    # completion imports resolve_profile at module level; gateway._plan imports
    # it from src.llm.profiles at call time — patch both bindings.
    monkeypatch.setattr(completion, "resolve_profile", lambda s, ws="default": local)
    monkeypatch.setattr(
        "src.llm.profiles.resolve_profile", lambda s, ws="default": local,
    )

    p = completion._routed("chat", "ws-a")
    assert p == local
    assert p.via_gateway is False
    no_proxy_http.assert_not_called()
