"""A workspace's own LiteLLM proxy (provider "litellm") — the call path.

What must hold:

    1. Resolution — the key and the base URL come from ONE row in ws:{id}, or
       from the env pair LITELLM_API_KEY + LITELLM_API_BASE; never half from
       each. A non-default workspace never reads another's row.
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
    6. The ping client allowlists a public proxy host, never a private one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.llm import litellm_proxy
from src.llm.profiles import Profile

BASE = "https://litellm.example.com"
KEY = "sk-virtual-key-1234567890"
ALIAS = "team-flash-lite-latest"


class _FakeStore:
    def __init__(self):
        self.rows: dict[tuple[str, str, str], SimpleNamespace] = {}

    def save(self, *, provider, secret, metadata=None, user_id="", account_label="default"):
        self.rows[(provider, user_id, account_label)] = SimpleNamespace(
            secret=secret, metadata=metadata or {},
        )

    def load(self, *, provider, user_id="", account_label="default"):
        return self.rows.get((provider, user_id, account_label))


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE",
                "LITELLM_API_KEY", "LITELLM_API_BASE", "OPENAI_COMPATIBLE_API_KEY",
                "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    litellm_proxy.reset_cache()
    yield
    litellm_proxy.reset_cache()


@pytest.fixture
def store():
    fake = _FakeStore()
    with patch("src.credentials.get_credential_store", return_value=fake):
        yield fake


def _proxy_profile(surface: str = "chat", api_base: str | None = BASE,
                   model: str = ALIAS) -> Profile:
    return Profile(surface=surface, provider="litellm", model=model,
                   api_key=KEY, raw_api_key=KEY, api_base=api_base)


# ─── 1. Resolution ────────────────────────────────────────────────────


def test_the_pair_resolves_from_the_workspace_row(store):
    litellm_proxy.save_endpoint("ws-a", base_url=BASE, api_key=KEY)
    ep = litellm_proxy.resolve_endpoint("ws-a")
    assert (ep.base_url, ep.api_key, ep.source) == (BASE, KEY, "ui")


def test_another_workspace_does_not_see_the_row(store):
    litellm_proxy.save_endpoint("ws-a", base_url=BASE, api_key=KEY)
    assert litellm_proxy.resolve_endpoint("ws-b") is None
    from src.llm.keys import LLMCredentialError, has_key, resolve_api_key
    assert has_key("litellm", workspace_id="ws-b") is False
    with pytest.raises(LLMCredentialError):
        resolve_api_key("litellm", workspace_id="ws-b")


def test_a_row_without_a_url_is_not_a_credential(store):
    store.save(provider="litellm", secret=KEY, user_id="ws:ws-a")
    assert litellm_proxy.resolve_endpoint("ws-a") is None


def test_the_env_pair_is_used_only_as_a_pair(store, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", KEY)
    assert litellm_proxy.resolve_endpoint("ws-a") is None
    monkeypatch.setenv("LITELLM_API_BASE", BASE + "/")
    ep = litellm_proxy.resolve_endpoint("ws-a")
    assert (ep.base_url, ep.source) == (BASE, "env")


def test_a_stored_row_beats_the_env_pair(store, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "sk-env-house-key-000000")
    monkeypatch.setenv("LITELLM_API_BASE", "https://house.example.com")
    litellm_proxy.save_endpoint("ws-a", base_url=BASE, api_key=KEY)
    ep = litellm_proxy.resolve_endpoint("ws-a")
    assert (ep.base_url, ep.api_key) == (BASE, KEY)


def test_the_profile_carries_the_pair(store, monkeypatch):
    from src.llm import profiles

    litellm_proxy.save_endpoint("ws-a", base_url=BASE, api_key=KEY)
    blob = {"profiles": {"review": {"provider": "litellm", "model": ALIAS}}}
    monkeypatch.setattr(profiles, "_blob", lambda workspace_id="default": blob)
    p = profiles.resolve_profile("review", "ws-a")
    assert (p.api_base, p.api_key) == (BASE, KEY)
    assert p.litellm_model == f"litellm_proxy/{ALIAS}"
    assert p.via_gateway is False


def test_shared_embeddings_never_use_the_callers_proxy(store, monkeypatch):
    """The alias means what the DEFAULT tenant's proxy maps it to; a tenant's
    own proxy must not answer for the shared collection."""
    from src.llm import profiles

    litellm_proxy.save_endpoint("ws-a", base_url=BASE, api_key=KEY)
    blob = {"profiles": {"embeddings": {"provider": "litellm",
                                        "model": "embedding-2-test"}}}
    monkeypatch.setattr(profiles, "_blob", lambda workspace_id="default": blob)
    p = profiles.resolve_profile("embeddings", "ws-a")
    assert p.api_base is None and p.api_key == ""

    litellm_proxy.save_endpoint("default", base_url="https://shared.example.com",
                                api_key="sk-default-key-1234567")
    p = profiles.resolve_profile("embeddings", "ws-a")
    assert p.api_base == "https://shared.example.com"


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
    import httpx

    body = {"data": [{"model_name": ALIAS,
                      "litellm_params": {"model": "gemini/gemini-2.5-flash-lite"},
                      "model_info": {"mode": "chat"}}]}

    def client(status, payload):
        return lambda base, *, timeout: httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(status, json=payload)))

    ep = litellm_proxy.Endpoint(base_url=BASE, api_key=KEY, source="ui")
    with patch("src.llm.litellm_proxy.ping_client", side_effect=client(200, body)):
        info = litellm_proxy.fetch_model_info(ep)
    assert info[ALIAS]["underlying"] == "gemini/gemini-2.5-flash-lite"
    with patch("src.llm.litellm_proxy.ping_client", side_effect=client(403, {})):
        assert litellm_proxy.fetch_model_info(ep) == {}


# ─── 5. list_configured_providers ────────────────────────────────────


def test_an_unconfigured_self_hosted_server_is_not_listed(store):
    from src.llm.keys import list_configured_providers

    assert "openai_compatible" not in list_configured_providers(workspace_id="ws-a")
    litellm_proxy.save_endpoint("ws-a", base_url=BASE, api_key=KEY)
    assert "litellm" in list_configured_providers(workspace_id="ws-a")
    assert "litellm" not in list_configured_providers(workspace_id="ws-b")


# ─── 6. Egress policy for the probe ──────────────────────────────────


@pytest.mark.parametrize("host, allowed", [
    ("8.8.8.8", True),
    ("127.0.0.1", False),
    ("169.254.169.254", False),
    ("10.0.0.5", False),
])
def test_only_a_public_proxy_host_extends_the_allowlist(host, allowed):
    with patch("src.http.build_client") as bc:
        litellm_proxy.ping_client(f"http://{host}:4000", timeout=1.0)
    extra = bc.call_args.kwargs["extra_allowed_hosts"]
    assert (host in extra) is allowed
