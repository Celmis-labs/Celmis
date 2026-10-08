"""A workspace-proxy alias is judged by the model the proxy runs it on.

``litellm_proxy/<alias>`` is the operator's own name for a deployment, so the
installed LiteLLM has no entry for it. Asked directly, the alias answered
``known=False`` and /settings/llm disabled the Reasoning select — while the
review told the operator to turn per-agent reasoning on. The proxy's
``/model/info`` says what the alias runs on; these tests stand that answer in
without a network and pin three things: the screen reads the underlying
model, a proxy that cannot answer leaves today's "unknown", and the saved
reasoning value is on the request that goes out.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import llm as llm_router
from src.llm import litellm_proxy
from src.llm.capabilities import reasoning_kwargs, reset_capability_caches

ALIAS = "litellm_proxy/3.5-flash-lite-myalias"
UNDERLYING = "gemini/gemini-3-flash-preview"

_USER = SimpleNamespace(id="u-lead", email="lead@test", is_admin=True)
_ENDPOINT = litellm_proxy.Endpoint(
    base_url="https://proxy.example.test", api_key="sk-fake-key-for-tests", source="ui",
)


@pytest.fixture(autouse=True)
def _fresh_caches():
    reset_capability_caches()
    litellm_proxy.reset_cache()
    yield
    reset_capability_caches()
    litellm_proxy.reset_cache()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(llm_router.router)
    app.dependency_overrides[get_current_user] = lambda: _USER
    app.dependency_overrides[current_workspace_id] = lambda: "default"
    return TestClient(app)


def _proxy_says(alias_to_underlying: dict[str, str]):
    """Stand in for GET /model/info on the one endpoint the workspace holds."""
    info = {a: {"underlying": u} for a, u in alias_to_underlying.items()}
    return (
        patch.object(litellm_proxy, "resolve_endpoint", return_value=_ENDPOINT),
        patch.object(litellm_proxy, "fetch_model_info", return_value=info),
    )


def _get(client: TestClient, model: str) -> dict:
    resp = client.get("/api/llm/model-capabilities", params={"model": model})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_an_alias_reports_the_capabilities_of_the_model_behind_it():
    resolve, fetch = _proxy_says({"3.5-flash-lite-myalias": UNDERLYING})
    with resolve, fetch:
        body = _get(_client(), ALIAS)

    assert body["known"] is True
    assert body["model"] == ALIAS
    assert body["resolved_from"] == UNDERLYING
    assert body["reasoning_kind"] == "effort"
    assert body["reasoning_values_router_accepts"]


def test_a_proxy_that_cannot_answer_leaves_the_alias_unknown_without_raising():
    with patch.object(litellm_proxy, "resolve_endpoint", return_value=_ENDPOINT), \
            patch.object(litellm_proxy, "request_json",
                         side_effect=litellm_proxy.LiteLLMProxyError("unreachable")):
        body = _get(_client(), ALIAS)

    assert body["known"] is False
    assert body["reasoning_kind"] is None
    assert body["resolved_from"] is None


def test_an_unconfigured_workspace_or_an_unlisted_alias_stays_unknown():
    with patch.object(litellm_proxy, "resolve_endpoint", return_value=None):
        assert _get(_client(), ALIAS)["known"] is False

    resolve, fetch = _proxy_says({"some-other-alias": UNDERLYING})
    with resolve, fetch:
        assert _get(_client(), ALIAS)["known"] is False


def test_an_underlying_model_litellm_does_not_know_is_not_guessed_at():
    resolve, fetch = _proxy_says({"3.5-flash-lite-myalias": "openai/not-in-any-catalogue"})
    with resolve, fetch:
        body = _get(_client(), ALIAS)

    assert body["known"] is False
    assert body["resolved_from"] is None


def test_a_failed_proxy_read_is_not_remembered_for_long(monkeypatch):
    """The empty answer expires on the short TTL, so a proxy that was down for
    a minute does not leave the alias unknown for the hour a good answer lasts."""
    calls = iter([{}, {"3.5-flash-lite-myalias": {"underlying": UNDERLYING}}])
    clock = [1000.0]
    monkeypatch.setattr(litellm_proxy.time, "monotonic", lambda: clock[0])
    with patch.object(litellm_proxy, "resolve_endpoint", return_value=_ENDPOINT), \
            patch.object(litellm_proxy, "fetch_model_info", side_effect=lambda ep: next(calls)):
        client = _client()
        assert _get(client, ALIAS)["known"] is False
        clock[0] += litellm_proxy._MODEL_INFO_EMPTY_TTL + 1
        assert _get(client, ALIAS)["known"] is True


def test_reasoning_is_accepted_at_save_time_for_an_alias_agent():
    from fastapi import HTTPException

    resolve, fetch = _proxy_says({"3.5-flash-lite-myalias": UNDERLYING})
    entry = {"reasoning": "low"}
    with resolve, fetch:
        llm_router._validate_agent_entry("security", entry, ALIAS)

    # …and with the proxy silent the old refusal still stands.
    litellm_proxy.reset_cache()
    with patch.object(litellm_proxy, "resolve_endpoint", return_value=None), \
            pytest.raises(HTTPException) as exc:
        llm_router._validate_agent_entry("security", {"reasoning": "low"}, ALIAS)
    assert exc.value.status_code == 422


def test_the_saved_reasoning_value_is_on_the_request_for_an_alias_agent():
    """The call still goes to the alias; the reasoning shape is the underlying
    model's, because that is the model the parameter is translated for."""
    from src.llm.client import LLMClient

    response = MagicMock()
    choice = MagicMock()
    choice.message.content = "ok"
    choice.finish_reason = "stop"
    response.choices = [choice]
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = 5
    response.usage.total_cost = None

    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return response

    expected = reasoning_kwargs(UNDERLYING, "low")
    assert expected, "the underlying model no longer takes an effort word — test is vacuous"

    with patch("litellm.completion", side_effect=fake_completion):
        LLMClient(
            resolve_key=lambda p: "sk-fake-key-for-tests",
            resolve_billing_model=lambda m: UNDERLYING if m == ALIAS else m,
        ).generate(
            model=ALIAS, prompt="hi", mode="review", operation="t", reasoning="low",
        )

    assert captured["model"] == ALIAS
    for key, value in expected.items():
        assert captured[key] == value
