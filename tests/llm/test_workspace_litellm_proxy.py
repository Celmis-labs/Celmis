"""A workspace's own LiteLLM proxy (provider "litellm") — the call path.

What must hold:

    1. Resolution — the key and the base URL come from ONE encrypted row in
       ws:{id}; there is no env fallback. A non-default workspace never reads
       another's row.
    2. Routing — chat, review/agent (LLMClient) and embeddings go out as
       "litellm_proxy/<alias>" with api_base = the proxy and the virtual key.
       No address → refuse (the SDK would otherwise read
       LITELLM_PROXY_API_BASE, the installation's gateway).
    3. The installation gateway never wraps a proxy profile (_attach_gateway,
       _plan, ensure_workspace_keys).
    4. Pricing — the alias is priced off /model/info's underlying model; an
       unanswered /model/info leaves the cost unknown and does not fail.
    5. list_configured_providers no longer reports a self-hosted server that
       nobody configured.
    6. Every SDK call re-validates the proxy host first (cached briefly): a
       name that now resolves to a private address is refused.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.llm import litellm_proxy
from src.llm.profiles import Profile

BASE = "https://litellm.example.com"
KEY = "sk-virtual-key-1234567890"
ALIAS = "team-flash-lite-latest"


#: Hermetic DNS: what each test hostname "resolves" to.
DNS = {"litellm.example.com": ["93.184.216.34"],
       "shared.example.com": ["93.184.216.35"]}


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE",
                "OPENAI_COMPATIBLE_API_KEY",
                "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(litellm_proxy, "_resolve",
                        lambda host, port: list(DNS.get(host, [host])))
    litellm_proxy.reset_cache()
    yield
    litellm_proxy.reset_cache()


@pytest.fixture
def store(tmp_path):
    from cryptography.fernet import Fernet

    from src.credentials.store import CredentialStore

    real = CredentialStore(tmp_path / "creds.db", Fernet.generate_key())
    with patch("src.credentials.get_credential_store", return_value=real):
        yield real


def _save(ws: str, base: str = BASE, key: str = KEY) -> None:
    litellm_proxy.save_endpoint(ws, litellm_proxy.Endpoint(base_url=base, api_key=key))


def _proxy_profile(surface: str = "chat", api_base: str | None = BASE,
                   model: str = ALIAS) -> Profile:
    return Profile(surface=surface, provider="litellm", model=model,
                   api_key=KEY, raw_api_key=KEY, api_base=api_base)


# ─── 1. Resolution ────────────────────────────────────────────────────


def test_the_pair_resolves_from_the_workspace_row(store):
    _save("ws-a")
    ep = litellm_proxy.resolve_endpoint("ws-a")
    assert (ep.base_url, ep.api_key, ep.source) == (BASE, KEY, "ui")


def test_another_workspace_does_not_see_the_row(store):
    _save("ws-a")
    assert litellm_proxy.resolve_endpoint("ws-b") is None
    from src.llm.keys import LLMCredentialError, has_key, resolve_api_key
    assert has_key("litellm", workspace_id="ws-b") is False
    with pytest.raises(LLMCredentialError):
        resolve_api_key("litellm", workspace_id="ws-b")


def test_a_bare_key_row_is_not_a_credential(store):
    # The pre-encrypted-URL shape (key as the secret, URL in metadata) is not
    # read: the URL must come out of the encrypted secret.
    store.save(provider="litellm", secret=KEY, user_id="ws:ws-a",
               metadata={"base_url": BASE})
    assert litellm_proxy.resolve_endpoint("ws-a") is None


def test_there_is_no_env_fallback(store, monkeypatch):
    from src.llm.keys import has_key

    monkeypatch.setenv("LITELLM_API_KEY", KEY)
    monkeypatch.setenv("LITELLM_API_BASE", BASE)
    assert litellm_proxy.resolve_endpoint("ws-a") is None
    assert litellm_proxy.resolve_endpoint("default") is None
    assert has_key("litellm", workspace_id="ws-a") is False


def test_the_profile_carries_the_pair(store, monkeypatch):
    from src.llm import profiles

    _save("ws-a")
    blob = {"profiles": {"review": {"provider": "litellm", "model": ALIAS}}}
    monkeypatch.setattr(profiles, "_blob", lambda workspace_id="default": blob)
    p = profiles.resolve_profile("review", "ws-a")
    assert (p.api_base, p.api_key) == (BASE, KEY)
    assert p.litellm_model == f"litellm_proxy/{ALIAS}"
    assert p.via_gateway is False


def test_shared_embeddings_never_use_the_callers_proxy(store, monkeypatch):
    """The alias means what the INSTALLATION embeddings proxy (the default
    tenant's row) maps it to; a tenant's own proxy must not answer for the
    shared collection — from whichever workspace the profile was saved or is
    resolved, now that a global admin can save it from any of them."""
    from src.llm import profiles

    _save("ws-a")
    blob = {"profiles": {"embeddings": {"provider": "litellm",
                                        "model": "embedding-2-test"}}}
    seen: list[str] = []

    def _blob(workspace_id="default"):
        seen.append(workspace_id)
        return blob

    monkeypatch.setattr(profiles, "_blob", _blob)
    p = profiles.resolve_profile("embeddings", "ws-a")
    # No installation proxy yet: fail closed (no address, no key) rather than
    # borrow ws-a's — which IS connected and would happily answer.
    assert p.api_base is None and p.api_key == ""
    # And the profile itself was read from the default tenant, not ws-a.
    assert seen and set(seen) == {"default"}

    _save("default", "https://shared.example.com", "sk-default-key-1234567")
    for caller in ("ws-a", "vp-test", "default"):
        p = profiles.resolve_profile("embeddings", caller)
        assert p.api_base == "https://shared.example.com"
        assert p.api_key == "sk-default-key-1234567"
        assert p.api_key != KEY
    # The tenant's own proxy is still its own for chat — only the shared
    # surface is pinned to the installation row.
    monkeypatch.setattr(profiles, "_blob", lambda workspace_id="default": {
        "profiles": {"chat": {"provider": "litellm", "model": ALIAS}}})
    assert profiles.resolve_profile("chat", "ws-a").api_base == BASE


# ─── 2. Routing ───────────────────────────────────────────────────────


def _consume(agen):
    async def _run():
        return [c async for c in agen]
    return asyncio.run(_run())


def _stream():
    async def _gen():
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = "proxied"
        chunk.usage = None
        yield chunk
    return _gen()


def test_the_chat_stream_goes_to_the_proxy():
    import litellm

    from src.llm.completion import _litellm_stream

    captured: dict = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _stream()

    with patch.object(litellm, "acompletion", side_effect=fake), \
         patch("src.llm.budget.record_spend"):
        out = _consume(_litellm_stream(
            _proxy_profile(), prompt="hi", system_instruction=None,
            temperature=None, max_output_tokens=None,
        ))
    assert out == ["proxied"]
    assert captured["model"] == f"litellm_proxy/{ALIAS}"
    assert captured["api_base"] == BASE
    assert captured["api_key"] == KEY


def test_the_chat_stream_refuses_without_an_address():
    import litellm

    from src.llm.completion import _litellm_stream

    with patch.object(litellm, "acompletion") as call, \
            pytest.raises(litellm_proxy.LiteLLMProxyError):
        _consume(_litellm_stream(
            _proxy_profile(api_base=None), prompt="hi", system_instruction=None,
            temperature=None, max_output_tokens=None,
        ))
    call.assert_not_called()


def test_embedding_kwargs_name_the_proxy():
    from src.llm.completion import _embedding_kwargs

    p = Profile(surface="embeddings", provider="litellm", model="embedding-2-test",
                api_key=KEY, raw_api_key=KEY, api_base=BASE, dimensions=768)
    kw = _embedding_kwargs(p, ["x"], "RETRIEVAL_DOCUMENT")
    assert kw["model"] == "litellm_proxy/embedding-2-test"
    assert kw["api_base"] == BASE
    assert kw["api_key"] == KEY
    assert "task_type" not in kw          # Gemini-only field, see _expressible_task_type
    with pytest.raises(litellm_proxy.LiteLLMProxyError):
        _embedding_kwargs(Profile(surface="embeddings", provider="litellm",
                                  model="e", api_key=KEY), ["x"], "")


def _response(text: str):
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = text
    choice.finish_reason = "stop"
    resp.choices = [choice]
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 5
    resp.usage.total_cost = None
    return resp


def test_the_review_client_sends_the_alias_to_the_proxy():
    from src.llm.client import build_llm_client

    captured: dict = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    with patch("src.llm.completion._routed", return_value=_proxy_profile("review")), \
         patch("litellm.completion", side_effect=fake), \
         patch("src.llm.litellm_proxy.cached_model_info", return_value={}), \
         patch("src.llm.budget.record_spend"):
        client = build_llm_client("u1", "ws-a", surface="review",
                                  resolve_model=lambda _agent: ALIAS)
        result = client.generate(agent="architect", prompt="hi", mode="review",
                                 operation="t")
    assert result.text == "ok"
    assert captured["model"] == f"litellm_proxy/{ALIAS}"
    assert captured["api_base"] == BASE
    assert captured["api_key"] == KEY


def test_the_review_client_refuses_without_an_address():
    from src.llm.client import build_llm_client

    with patch("src.llm.completion._routed",
               return_value=_proxy_profile("review", api_base=None)), \
         patch("litellm.completion") as call:
        client = build_llm_client("u1", "ws-a", surface="review",
                                  resolve_model=lambda _agent: ALIAS)
        with pytest.raises(litellm_proxy.LiteLLMProxyError):
            client.generate(agent="architect", prompt="hi", mode="review",
                            operation="t")
    call.assert_not_called()


def _review_call(profile, *, monkeypatch, model=None, resolver=None):
    """Run one review-client call against `profile`; return litellm's kwargs."""
    from src.llm.client import build_llm_client

    monkeypatch.setenv("OPENAI_API_KEY", "sk-operator-openai-key-000000")
    captured: dict = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return _response("ok")

    with patch("src.llm.completion._routed", return_value=profile), \
         patch("litellm.completion", side_effect=fake), \
         patch("src.llm.litellm_proxy.cached_model_info", return_value={}), \
         patch("src.llm.budget.record_spend"):
        client = build_llm_client("u1", "ws-a", surface="review",
                                  resolve_model=resolver)
        client.generate(agent="architect", model=model, prompt="hi",
                        mode="review", operation="t")
    return captured


def test_an_explicit_review_model_still_goes_to_the_proxy(monkeypatch):
    # The review agents name the model themselves (the mirrored review model,
    # the fallback) — that must not skip the proxy for api.openai.com.
    kw = _review_call(_proxy_profile("review"), monkeypatch=monkeypatch, model=ALIAS)
    assert kw["model"] == f"litellm_proxy/{ALIAS}"
    assert kw["api_base"] == BASE
    assert kw["api_key"] == KEY


@pytest.mark.parametrize("alias", ["openai/gpt-4o", "gemini/gemini-2.5-flash",
                                   "anthropic/claude-x"])
def test_a_slash_alias_goes_to_the_proxy(monkeypatch, alias):
    for kw in (
        _review_call(_proxy_profile("review"), monkeypatch=monkeypatch,
                     resolver=lambda _a: alias),
        _review_call(_proxy_profile("review"), monkeypatch=monkeypatch, model=alias),
    ):
        assert kw["model"] == f"litellm_proxy/{alias}"
        assert kw["api_base"] == BASE
        assert kw["api_key"] == KEY


def test_the_capability_lookup_prefixes_a_slash_alias():
    from src.llm.capabilities import resolve_litellm_model

    assert resolve_litellm_model("openai/gpt-4o", "litellm") == "litellm_proxy/openai/gpt-4o"
    assert resolve_litellm_model(f"litellm_proxy/{ALIAS}", "litellm") == f"litellm_proxy/{ALIAS}"
    # Off the proxy, a prefixed name is still left alone.
    assert resolve_litellm_model("openai/gpt-4o", "openai") == "openai/gpt-4o"


def test_an_explicit_model_off_the_proxy_is_untouched(monkeypatch):
    p = Profile(surface="review", provider="openai", model="gpt-4o",
                api_key="sk-x", raw_api_key="sk-x")
    kw = _review_call(p, monkeypatch=monkeypatch, model="gpt-4o")
    assert kw["model"] == "gpt-4o"
    assert "api_base" not in kw or kw["api_base"] is None


# ─── 3. The installation gateway keeps out ───────────────────────────


def test_the_gateway_never_wraps_a_proxy_profile(monkeypatch):
    from src.llm import gateway
    from src.llm.profiles import _attach_gateway

    monkeypatch.setenv("LITELLM_PROXY_URL", "http://litellm:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test-key")
    route = MagicMock(virtual_key="sk-gw", deployment="celmis-ws-a-chat",
                      base_url="http://litellm:4000", underlying_model="x")
    with patch.object(gateway, "route_for", return_value=route) as rf:
        p = _attach_gateway(_proxy_profile(), "ws-a")
    rf.assert_not_called()
    assert p.api_key == KEY and p.via_gateway is False

    with patch("src.llm.profiles.resolve_profile",
               side_effect=lambda s, ws: _proxy_profile(s)):
        assert gateway._plan("ws-a") == []
    assert gateway.ensure_workspace_keys(
        "ws-a", provider="litellm", real_api_key=KEY, models={"chat": ALIAS},
    ) is None


# ─── 4. Pricing ───────────────────────────────────────────────────────


def test_an_alias_is_priced_off_its_underlying_model():
    from src.llm.completion import _record_litellm_spend

    info = {ALIAS: {"underlying": "gemini/gemini-2.5-flash-lite", "mode": "chat"}}
    with patch("src.llm.litellm_proxy.cached_model_info", return_value=info), \
         patch("src.llm.budget.record_spend") as spend:
        _record_litellm_spend(_proxy_profile(), operation="answer_streaming",
                              workspace_id="ws-a", tokens_in=1000, tokens_out=1000)
    kw = spend.call_args.kwargs
    assert kw["provider"] == "litellm"
    assert kw["cost_source"] == "litellm_estimate"
    assert kw["cost_usd"] > 0


def test_an_unknown_alias_costs_nothing_and_does_not_fail():
    from src.llm.completion import _record_litellm_spend

    with patch("src.llm.litellm_proxy.cached_model_info", return_value={}), \
         patch("src.llm.budget.record_spend") as spend:
        _record_litellm_spend(_proxy_profile(), operation="answer_streaming",
                              workspace_id="ws-a", tokens_in=10, tokens_out=10)
    assert spend.call_args.kwargs["cost_source"] == "unknown"


def test_model_info_is_parsed_and_a_refusal_is_empty():
    body = {"data": [{"model_name": ALIAS,
                      "litellm_params": {"model": "gemini/gemini-2.5-flash-lite"},
                      "model_info": {"mode": "chat"}}]}
    ep = litellm_proxy.Endpoint(base_url=BASE, api_key=KEY, source="ui")
    with patch("src.llm.litellm_proxy.request_json", return_value=(200, body)) as rq:
        info = litellm_proxy.fetch_model_info(ep)
    assert info[ALIAS]["underlying"] == "gemini/gemini-2.5-flash-lite"
    assert rq.call_args.args[0].addresses == ("93.184.216.34",)   # validated target
    with patch("src.llm.litellm_proxy.request_json", return_value=(403, {})):
        assert litellm_proxy.fetch_model_info(ep) == {}


# ─── 5. list_configured_providers ────────────────────────────────────


def test_an_unconfigured_self_hosted_server_is_not_listed(store):
    from src.llm.keys import list_configured_providers

    assert "openai_compatible" not in list_configured_providers(workspace_id="ws-a")
    _save("ws-a")
    assert "litellm" in list_configured_providers(workspace_id="ws-a")
    assert "litellm" not in list_configured_providers(workspace_id="ws-b")


# ─── 6. Call-time re-validation ──────────────────────────────────────


def test_an_sdk_call_is_refused_once_the_host_turns_private(monkeypatch):
    import litellm

    from src.llm.completion import _litellm_stream

    DNS["rebind.example.com"] = ["93.184.216.40"]
    base = "https://rebind.example.com"
    try:
        assert litellm_proxy.require_api_base(base) == base
        DNS["rebind.example.com"] = ["10.0.0.7"]
        litellm_proxy.reset_cache()            # past the short TTL
        with patch.object(litellm, "acompletion") as call, \
                pytest.raises(litellm_proxy.UnsafeProxyURL):
            _consume(_litellm_stream(
                _proxy_profile(api_base=base), prompt="hi",
                system_instruction=None, temperature=None, max_output_tokens=None,
            ))
        call.assert_not_called()
    finally:
        DNS.pop("rebind.example.com", None)


def test_the_call_time_check_is_cached_briefly(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(litellm_proxy, "_resolve",
                        lambda host, port: calls.append(host) or ["93.184.216.34"])
    clock = [500.0]
    monkeypatch.setattr(litellm_proxy.time, "monotonic", lambda: clock[0])
    for _ in range(5):
        litellm_proxy.require_api_base(BASE)
    assert calls == ["litellm.example.com"]
    clock[0] += 31
    litellm_proxy.require_api_base(BASE)
    assert len(calls) == 2


def test_the_gateway_cost_is_not_estimated(monkeypatch):
    """Installation-gateway calls keep recording an unknown cost, as before;
    only a workspace proxy is priced off its alias."""
    from src.llm.client import build_llm_client

    gw = Profile(surface="review", provider="google", model="gemini-3-flash",
                 api_key="sk-gw", raw_api_key="sk-gw",
                 gateway_model="celmis-ws-a-review", gateway_url="http://litellm:4000",
                 gateway_underlying="gemini/gemini-2.5-flash")
    no_cost = patch("src.llm.pricing.extract_actual_cost_usd",
                    return_value=(None, "unknown"))
    with patch("src.llm.completion._routed", return_value=gw), \
         patch("litellm.completion", return_value=_response("ok")), \
         no_cost, patch("src.llm.budget.record_spend") as spend:
        client = build_llm_client("u1", "ws-a", surface="review",
                                  resolve_model=lambda _a: "x")
        client.generate(agent="architect", prompt="hi", mode="review", operation="t")
    assert spend.call_args.kwargs["cost_source"] == "unknown"
    assert spend.call_args.kwargs["cost_usd"] in (None, 0, 0.0)

    info = {ALIAS: {"underlying": "gemini/gemini-2.5-flash", "mode": "chat"}}
    with patch("src.llm.completion._routed", return_value=_proxy_profile("review")), \
         patch("litellm.completion", return_value=_response("ok")), \
         patch("src.llm.litellm_proxy.cached_model_info", return_value=info), \
         patch("src.llm.pricing.extract_actual_cost_usd",
               return_value=(None, "unknown")), \
         patch("src.llm.budget.record_spend") as spend:
        client = build_llm_client("u1", "ws-a", surface="review",
                                  resolve_model=lambda _a: ALIAS)
        client.generate(agent="architect", prompt="hi", mode="review", operation="t")
    assert spend.call_args.kwargs["cost_source"] == "litellm_estimate"


def test_an_empty_model_info_is_retried_soon(monkeypatch):
    ep = litellm_proxy.Endpoint(base_url=BASE, api_key=KEY, source="ui")
    clock = [1000.0]
    monkeypatch.setattr(litellm_proxy.time, "monotonic", lambda: clock[0])
    answers = [{}, {ALIAS: {"underlying": "u", "mode": "chat"}}]
    with patch("src.llm.litellm_proxy.fetch_model_info",
               side_effect=lambda *_a, **_k: answers.pop(0)) as fetch:
        assert litellm_proxy.cached_model_info(ep) == {}
        clock[0] += 30
        assert litellm_proxy.cached_model_info(ep) == {}       # still cached
        clock[0] += 60
        assert ALIAS in litellm_proxy.cached_model_info(ep)    # retried
        clock[0] += 1800
        assert ALIAS in litellm_proxy.cached_model_info(ep)    # positive: 1 h
    assert fetch.call_count == 2
