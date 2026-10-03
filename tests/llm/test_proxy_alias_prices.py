"""The price of a workspace-proxy alias — one resolver, every cost path.

What must hold (src/llm/proxy_pricing.py):

    1. Order: manual price → proxy-declared price → LiteLLM table price of the
       underlying model → unknown. USD per 1M tokens stored, per token billed.
    2. A proxy price that is missing / None / negative / NaN / a string is
       ignored; an explicit 0 is a price.
    3. Every cost path uses it: the chat stream / embeddings ledger
       (`_record_litellm_spend`) and the review / agent `LLMClient.generate`,
       whose LLMResult and ledger row carry the same figure.
    4. Embeddings take the DEFAULT workspace's manual price — they run on the
       default workspace's proxy whoever calls.
    5. The installation gateway is untouched.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pytest

from src.llm import litellm_proxy, proxy_pricing
from src.llm.profiles import Profile

BASE = "https://litellm.example.com"
KEY = "sk-virtual-key-1234567890"
ALIAS = "team-custom-latest"
ENDPOINT = litellm_proxy.Endpoint(base_url=BASE, api_key=KEY, source="profile")
# A model LiteLLM's table certainly knows.
TABLE_MODEL = "gpt-4o"


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    for var in ("LITELLM_PROXY_URL", "LITELLM_MASTER_KEY", "LITELLM_PROXY_API_BASE",
                "OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(litellm_proxy, "_resolve",
                        lambda host, port: ["93.184.216.34"])
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


def _set_manual(ws: str, alias: str, inp: float, out: float) -> None:
    from src.api.routers.llm import _load_workspace_config, _save_workspace_config

    cfg = _load_workspace_config(ws)
    cfg.setdefault("model_prices", {})[alias] = {
        "input_per_mtok": inp, "output_per_mtok": out,
        "updated_by": "t@test", "updated_at": "2026-10-03T00:00:00+00:00"}
    _save_workspace_config(cfg, updated_by="t", workspace_id=ws)


def _info(**model_info) -> dict:
    entry = {"underlying": model_info.pop("underlying", None),
             "mode": model_info.pop("mode", "chat"),
             "input_cost_per_token": None, "output_cost_per_token": None}
    entry.update(model_info)
    return {ALIAS: entry}


def _cost(ws: str = "ws-a", **kw):
    return proxy_pricing.workspace_proxy_cost(
        ALIAS, workspace_id=ws, endpoint=ENDPOINT,
        tokens_in=kw.pop("tokens_in", 1_000_000), tokens_out=kw.pop("tokens_out", 0),
        **kw)


def _table_input_per_token(model: str) -> float:
    from src.llm.pricing import get_pricing_resolver

    return get_pricing_resolver().get(model).input_usd_per_token


# ─── 1. Order ─────────────────────────────────────────────────────────


def test_manual_beats_proxy_beats_table_beats_unknown(store):
    full = _info(underlying=TABLE_MODEL, input_cost_per_token=2e-6,
                 output_cost_per_token=4e-6)
    with patch("src.llm.litellm_proxy.cached_model_info", return_value=full):
        _set_manual("ws-a", ALIAS, 7.0, 9.0)
        assert _cost() == (pytest.approx(7.0), "manual_price")

    with patch("src.llm.litellm_proxy.cached_model_info", return_value=full):
        assert _cost(ws="ws-b") == (pytest.approx(2.0), "proxy_price")

    table_only = _info(underlying=TABLE_MODEL)
    with patch("src.llm.litellm_proxy.cached_model_info", return_value=table_only):
        cost, source = _cost(ws="ws-b")
    assert source == "litellm_estimate"
    assert cost == pytest.approx(1_000_000 * _table_input_per_token(TABLE_MODEL))

    with patch("src.llm.litellm_proxy.cached_model_info",
               return_value=_info(underlying="acme/finetune-nobody-knows")):
        assert _cost(ws="ws-b") == (None, "unknown")


def test_per_million_is_converted_to_per_token(store):
    _set_manual("ws-a", ALIAS, 1.5, 6.0)
    with patch("src.llm.litellm_proxy.cached_model_info", return_value={}):
        cost, source = _cost(tokens_in=2000, tokens_out=500)
    assert source == "manual_price"
    assert cost == pytest.approx(2000 * 1.5e-6 + 500 * 6e-6)


def test_a_manual_price_outranks_what_the_response_estimated(store):
    _set_manual("ws-a", ALIAS, 1.0, 1.0)
    with patch("src.llm.litellm_proxy.cached_model_info", return_value={}):
        assert _cost(response_cost=123.0, response_source="litellm_estimate") \
            == (pytest.approx(1.0), "manual_price")


# ─── 2. Garbage from the proxy ────────────────────────────────────────


@pytest.mark.parametrize("bad", [None, -1e-6, float("nan"), float("inf"), "0.000002", True])
def test_a_garbage_proxy_price_is_ignored(bad):
    body = {"data": [{"model_name": ALIAS,
                      "litellm_params": {"model": "acme/unknown-model"},
                      "model_info": {"mode": "chat", "input_cost_per_token": bad,
                                     "output_cost_per_token": bad}}]}
    with patch("src.llm.litellm_proxy.request_json", return_value=(200, body)):
        info = litellm_proxy.fetch_model_info(ENDPOINT)
    assert info[ALIAS]["input_cost_per_token"] is None
    assert info[ALIAS]["output_cost_per_token"] is None
    with patch("src.llm.litellm_proxy.cached_model_info", return_value=info), \
         patch("src.llm.proxy_pricing.load_manual_prices", return_value={}):
        assert _cost() == (None, "unknown")


def test_a_missing_proxy_price_key_is_not_zero():
    body = {"data": [{"model_name": ALIAS, "litellm_params": {"model": "acme/x"},
                      "model_info": {"mode": "chat"}}]}
    with patch("src.llm.litellm_proxy.request_json", return_value=(200, body)):
        info = litellm_proxy.fetch_model_info(ENDPOINT)
    assert proxy_pricing.proxy_price(info[ALIAS]) is None


def test_an_explicit_proxy_zero_is_a_price():
    body = {"data": [{"model_name": ALIAS, "litellm_params": {"model": "acme/x"},
                      "model_info": {"mode": "chat", "input_cost_per_token": 0,
                                     "output_cost_per_token": 0.0}}]}
    with patch("src.llm.litellm_proxy.request_json", return_value=(200, body)):
        info = litellm_proxy.fetch_model_info(ENDPOINT)
    with patch("src.llm.litellm_proxy.cached_model_info", return_value=info), \
         patch("src.llm.proxy_pricing.load_manual_prices", return_value={}):
        assert _cost() == (0.0, "proxy_price")


def test_an_embedding_alias_needs_no_output_price():
    price = proxy_pricing.proxy_price(
        {"mode": "embedding", "input_cost_per_token": 1e-7,
         "output_cost_per_token": None})
    assert price is not None and price.output_per_token == 0.0
    assert proxy_pricing.proxy_price(
        {"mode": "chat", "input_cost_per_token": 1e-7}) is None


def test_clean_price_refuses_everything_but_finite_non_negative_numbers():
    assert proxy_pricing.clean_price(0) == 0.0
    assert proxy_pricing.clean_price(1e-6) == 1e-6
    for bad in (None, -0.1, math.nan, math.inf, "1", False, [], {}):
        assert proxy_pricing.clean_price(bad) is None


# ─── 3. The ledger path (chat stream, embeddings) ────────────────────


def _proxy_profile(surface: str = "chat") -> Profile:
    return Profile(surface=surface, provider="litellm", model=ALIAS,
                   api_key=KEY, raw_api_key=KEY, api_base=BASE)


def test_the_chat_ledger_row_carries_the_manual_price(store):
    from src.llm.completion import _record_litellm_spend

    _set_manual("ws-a", ALIAS, 2.0, 8.0)
    with patch("src.llm.litellm_proxy.cached_model_info", return_value={}), \
         patch("src.llm.budget.record_spend") as spend:
        _record_litellm_spend(_proxy_profile(), operation="answer_streaming",
                              workspace_id="ws-a", tokens_in=1000, tokens_out=1000)
    kw = spend.call_args.kwargs
    assert kw["cost_source"] == "manual_price"
    assert kw["cost_usd"] == pytest.approx(1000 * 2e-6 + 1000 * 8e-6)


def test_embeddings_take_the_default_workspaces_manual_price(store):
    from src.llm.completion import _record_litellm_spend

    _set_manual("default", ALIAS, 0.5, 0.0)
    _set_manual("ws-a", ALIAS, 99.0, 99.0)     # the caller's own: must NOT apply
    with patch("src.llm.litellm_proxy.cached_model_info", return_value={}), \
         patch("src.llm.budget.record_spend") as spend:
        _record_litellm_spend(_proxy_profile("embeddings"), operation="embed_batch",
                              workspace_id="ws-a", tokens_in=4000)
    kw = spend.call_args.kwargs
    assert kw["workspace_id"] == "ws-a"           # still billed to the caller
    assert kw["cost_source"] == "manual_price"
    assert kw["cost_usd"] == pytest.approx(4000 * 0.5e-6)


# ─── 4. The review / agent client, end to end ─────────────────────────


def _response(text: str = "ok"):
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = text
    choice.finish_reason = "stop"
    resp.choices = [choice]
    resp.usage.prompt_tokens = 10_000
    resp.usage.completion_tokens = 2_000
    resp.usage.total_cost = None
    resp.usage.cost = None
    resp.usage.prompt_tokens_details = None
    return resp


def test_generate_bills_a_manual_priced_alias_on_result_and_ledger(store):
    from src.llm.client import build_llm_client

    _set_manual("ws-a", ALIAS, 3.0, 15.0)
    want = 10_000 * 3e-6 + 2_000 * 15e-6
    with patch("src.llm.completion._routed", return_value=_proxy_profile("review")), \
         patch("litellm.completion", return_value=_response()), \
         patch("src.llm.litellm_proxy.cached_model_info",
               return_value=_info(underlying="acme/finetune")), \
         patch("src.llm.budget.record_spend") as spend:
        client = build_llm_client("u1", "ws-a", surface="review",
                                  resolve_model=lambda _a: ALIAS)
        result = client.generate(agent="architect", prompt="hi", mode="review",
                                 operation="t")
    assert result.cost_source == "manual_price"
    assert result.cost_usd == pytest.approx(want)
    kw = spend.call_args.kwargs
    assert kw["cost_source"] == "manual_price"
    assert kw["cost_usd"] == pytest.approx(want)


def test_generate_without_any_price_stays_unknown(store):
    from src.llm.client import build_llm_client

    with patch("src.llm.completion._routed", return_value=_proxy_profile("review")), \
         patch("litellm.completion", return_value=_response()), \
         patch("src.llm.litellm_proxy.cached_model_info",
               return_value=_info(underlying="acme/finetune")), \
         patch("src.llm.pricing.extract_actual_cost_usd", return_value=(None, "unknown")), \
         patch("src.llm.budget.record_spend") as spend:
        client = build_llm_client("u1", "ws-a", surface="review",
                                  resolve_model=lambda _a: ALIAS)
        result = client.generate(agent="architect", prompt="hi", mode="review",
                                 operation="t")
    assert (result.cost_usd, result.cost_source) == (None, "unknown")
    assert spend.call_args.kwargs["cost_source"] == "unknown"


# ─── 5. The installation gateway is untouched ─────────────────────────


def test_the_gateway_never_reads_manual_prices(store):
    from src.llm.client import build_llm_client

    _set_manual("ws-a", "celmis-ws-a-review", 50.0, 50.0)
    gw = Profile(surface="review", provider="google", model="gemini-3-flash",
                 api_key="sk-gw", raw_api_key="sk-gw",
                 gateway_model="celmis-ws-a-review", gateway_url="http://litellm:4000",
                 gateway_underlying="gemini/gemini-2.5-flash")
    with patch("src.llm.completion._routed", return_value=gw), \
         patch("litellm.completion", return_value=_response()), \
         patch("src.llm.pricing.extract_actual_cost_usd", return_value=(None, "unknown")), \
         patch("src.llm.proxy_pricing.workspace_proxy_cost") as resolver, \
         patch("src.llm.budget.record_spend") as spend:
        client = build_llm_client("u1", "ws-a", surface="review",
                                  resolve_model=lambda _a: "x")
        result = client.generate(agent="architect", prompt="hi", mode="review",
                                 operation="t")
    resolver.assert_not_called()
    assert result.cost_source == "unknown"
    assert spend.call_args.kwargs["cost_source"] == "unknown"
