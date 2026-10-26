"""The review model is asked, once, before a review spends anything.

Before this the first sign of a dead model was every agent failing, a
"REVIEW FAILED" comment on the pull request and a job marked completed. The
preflight sends one tiny real request through the client the review itself
will use, so that a review that cannot run never starts. Pinned here:

  * one probe per (workspace, model) for a whole burst of pull-request events;
    a yes is remembered longer than a no;
  * an outage, a throttle and a workspace-level block (key, budget, quota,
    unknown model) are "unavailable"; a reply, an empty reply and a failure
    that says nothing about reachability are "available";
  * the OpenRouter fallback counts: a model reachable only through it is
    available;
  * the probe is small — a few tokens, a short timeout, no litellm retries;
  * the reason is the curated sentence, never the exception's text;
  * the roster decides which models are asked, and the knob turns it off.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.llm.client import LLMClient
from src.review import model_preflight as mp
from src.review.model_preflight import ModelUnavailable, ReviewModels


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CELMIS_REVIEW_MODEL_PREFLIGHT", "on")
    mp.reset_cache()


class _Client:
    """An LLMClient double: records every probe, answers from a script."""

    def __init__(self, *, raises=None, text="ok", delay=0.0):
        self.calls: list[dict] = []
        self.raises = raises
        self.text = text
        self.delay = delay

    def generate(self, **kw):
        self.calls.append(kw)
        if self.delay:
            time.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(text=self.text)


def _models(client, *pairs):
    return ReviewModels(client, list(pairs or [("defect", "m-1")]))


def _litellm(name, *args):
    import litellm.exceptions as le

    return getattr(le, name)(*args)


# ─── one probe for a burst ───────────────────────────────────────────


def test_a_burst_of_events_costs_one_probe():
    client = _Client(delay=0.05)
    models = _models(client)
    results: list[mp.ModelCheck] = []

    def _go():
        results.append(mp.check_models("ws", models))

    threads = [threading.Thread(target=_go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(client.calls) == 1
    assert all(r.available for r in results)
    assert mp.check_models("ws", models).cached is True
    assert len(client.calls) == 1


def test_another_workspace_or_model_is_asked_separately():
    client = _Client()
    mp.check_models("ws-a", _models(client))
    mp.check_models("ws-b", _models(client))
    mp.check_models("ws-a", _models(client, ("defect", "m-2")))
    assert len(client.calls) == 3


def test_a_yes_is_remembered_longer_than_a_no():
    mp.check_models("ws", _models(_Client()))
    mp.check_models("ws2", _models(_Client(raises=_litellm(
        "ServiceUnavailableError", "down", "openai", "m-1"))))
    now = time.monotonic()
    ok = mp._CACHE[("ws", "m-1")][0] - now
    no = mp._CACHE[("ws2", "m-1")][0] - now
    assert 50 < ok <= 60 and 10 < no <= 15


def test_an_expired_answer_is_asked_again(monkeypatch):
    client = _Client()
    mp.check_models("ws", _models(client))
    clock = time.monotonic() + 61
    monkeypatch.setattr(mp.time, "monotonic", lambda: clock)
    mp.check_models("ws", _models(client))
    assert len(client.calls) == 2


# ─── what counts as unavailable ──────────────────────────────────────


@pytest.mark.parametrize("make,code", [
    (lambda: _litellm("ServiceUnavailableError", "x", "openai", "m"), "provider_unavailable"),
    (lambda: _litellm("InternalServerError", "x", "openai", "m"), "provider_unavailable"),
    (lambda: _litellm("Timeout", "x", "m", "openai"), "local_timeout"),
    (lambda: _litellm("RateLimitError", "You exceeded your current quota", "openai", "m"),
     "quota_exhausted"),
    (lambda: _litellm("AuthenticationError", "bad", "openai", "m"), "invalid_key"),
    (lambda: _litellm("NotFoundError", "no such model", "openai", "m"), "model_not_found"),
])
def test_an_outage_or_a_workspace_block_is_unavailable(make, code):
    check = mp.check_models("ws", _models(_Client(raises=make())))
    assert check.available is False and check.code == code
    assert check.reason


def test_a_429_on_the_probe_is_busy_not_down():
    check = mp.check_models("ws", _models(_Client(
        raises=_litellm("RateLimitError", "slow down", "openai", "m"))))
    assert check.available is True, "the review has its own retries; it gets its chance"


def test_the_probe_is_tiny_and_asks_for_the_least_reasoning():
    client = _Client()
    mp.check_models("ws", _models(client))
    call = client.calls[0]
    assert call["max_output_tokens"] == 16 and call["reasoning"] == "low"


def test_only_a_fixable_block_notifies_the_workspace_and_only_with_a_curated_reason(
        monkeypatch):
    import src.notifications as notifications

    sent = []
    monkeypatch.setattr(notifications, "notify", lambda **kw: sent.append(kw))
    payload = {"workspace_id": "ws", "repo": "o/r", "pr_number": 7}
    mp.notify_blocked(payload, "provider_unavailable", "the provider is unavailable")
    assert sent == [], "an outage fixes itself; nobody needs a message"
    mp.notify_blocked(payload, "budget_exceeded", "the budget is used up")
    [one] = sent
    assert one["workspace_id"] == "ws" and "the budget is used up" in one["body_md"]


def test_a_missing_key_and_an_exhausted_budget_are_unavailable():
    from src.llm.budget import BudgetExceeded
    from src.llm.keys import LLMCredentialError

    no_key = mp.check_models("ws", _models(_Client(raises=LLMCredentialError("none"))))
    assert (no_key.available, no_key.code) == (False, "no_api_key")
    mp.reset_cache()
    check = mp.check_models("ws", _models(_Client(raises=BudgetExceeded("ws", 10.0, 10.0))))
    assert (check.available, check.code) == (False, "budget_exceeded")


@pytest.mark.parametrize("make", [
    lambda: _litellm("ContextWindowExceededError", "too long", "m", "openai"),
    lambda: ValueError("something odd about this probe"),
])
def test_a_failure_that_says_nothing_about_reachability_is_available(make):
    check = mp.check_models("ws", _models(_Client(raises=make())))
    assert check.available is True


def test_a_reply_of_any_kind_is_available():
    for text in ("ok", "", "I cannot help with that request."):
        mp.reset_cache()
        assert mp.check_models("ws", _models(_Client(text=text))).available is True


def test_the_reason_is_the_curated_sentence_never_the_exceptions_text():
    exc = _litellm("ServiceUnavailableError",
                   "upstream said: key=sk-abcdefghijklmnop12345 host=10.0.0.5", "openai", "m")
    check = mp.check_models("ws", _models(_Client(raises=exc)))
    assert check.reason == "the provider is unavailable"
    assert "sk-" not in check.reason and "10.0.0.5" not in check.reason
    with pytest.raises(ModelUnavailable) as caught:
        check.raise_if_unavailable()
    assert caught.value.reason == check.reason and caught.value.code == check.code
    assert "sk-" not in str(caught.value)


def test_the_first_unavailable_model_decides_and_the_rest_are_not_asked():
    class _Mixed(_Client):
        def generate(self, **kw):
            self.calls.append(kw)
            if kw["agent"] == "defect":
                raise _litellm("ServiceUnavailableError", "x", "openai", "m")
            return SimpleNamespace(text="ok")

    client = _Mixed()
    check = mp.check_models("ws", _models(client, ("defect", "m-1"), ("security", "m-2")))
    assert check.available is False and len(client.calls) == 1


# ─── the probe is small and goes through the review's own client ─────


def test_the_probe_is_tiny_has_a_short_timeout_and_no_litellm_retries():
    client = _Client()
    mp.check_models("ws", _models(client, ("security", "m-1")))
    (call,) = client.calls
    assert call["agent"] == "security" and call["operation"] == "review_preflight"
    assert call["num_retries"] == 0
    assert call["timeout"] == 20
    assert 16 <= call["max_output_tokens"] <= 256
    assert len(call["prompt"]) < 100


def test_a_model_reachable_only_through_the_fallback_is_available(monkeypatch):
    """The direct provider has no key; the workspace enabled OpenRouter. The
    probe goes through `LLMClient.generate`, so the fallback is the review's."""
    import src.api.routers.llm as llm_router

    monkeypatch.setattr(llm_router, "_load_workspace_config",
                        lambda ws: {"openrouter_enabled": True})

    def _key(provider):
        if provider == "openrouter":
            return "sk-or-fake"
        raise RuntimeError("no direct key")

    client = LLMClient(resolve_key=_key,
                       resolve_model=lambda agent: "anthropic/claude-sonnet-5")
    resp = MagicMock()
    choice = MagicMock()
    choice.message.content = "ok"
    choice.finish_reason = "stop"
    resp.choices = [choice]
    resp.usage.prompt_tokens = 5
    resp.usage.completion_tokens = 1
    resp.usage.total_cost = None
    seen: list[dict] = []
    with patch("litellm.completion",
               side_effect=lambda **kw: (seen.append(kw), resp)[1]):
        check = mp.check_models("ws", ReviewModels(client, [("defect", "anthropic/claude-sonnet-5")]))
    assert check.available is True
    assert seen[0]["model"].startswith("openrouter/")
    assert seen[0]["num_retries"] == 0 and seen[0]["timeout"] == 20


def test_without_the_fallback_a_missing_key_is_an_outage(monkeypatch):
    import src.api.routers.llm as llm_router
    from src.llm.keys import LLMCredentialError

    monkeypatch.setattr(llm_router, "_load_workspace_config", lambda ws: {})

    def _key(provider):
        raise LLMCredentialError("no key")

    client = LLMClient(resolve_key=_key, resolve_model=lambda agent: "anthropic/claude-x")
    with patch("litellm.completion") as completion:
        check = mp.check_models("ws", ReviewModels(client, [("defect", "anthropic/claude-x")]))
    assert check.available is False and check.code == "no_api_key"
    completion.assert_not_called()


# ─── the roster, the knob, and failing open ──────────────────────────


def test_the_knob_turns_the_probe_off(monkeypatch):
    monkeypatch.setenv("CELMIS_REVIEW_MODEL_PREFLIGHT", "off")
    client = _Client(raises=_litellm("ServiceUnavailableError", "x", "openai", "m"))
    assert mp.check_models("ws", _models(client)).available is True
    assert mp.check_for_review("github", "o/r", workspace_id="ws", user_id="u").available
    assert client.calls == []


def test_a_question_that_cannot_be_asked_is_not_an_outage(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("database down")

    monkeypatch.setattr(mp, "review_models", _boom)
    assert mp.check_for_review("github", "o/r", workspace_id="ws", user_id="u").available


def test_the_models_asked_are_the_distinct_ones_the_roster_would_call(monkeypatch):
    import src.api.routers.llm as llm_router
    from src.review.orchestrator import ReviewOrchestrator

    by_agent = {"defect": "m-1", "contract": "m-1", "security": "m-2",
                "performance": "m-1", "verifier": "m-3", "compliance": "m-1",
                "business_logic": "m-9"}
    client = SimpleNamespace(model_for=lambda a: by_agent.get(a))
    monkeypatch.setattr(llm_router, "_load_workspace_config", lambda ws: {})
    monkeypatch.setattr(ReviewOrchestrator, "_resolved_policy",
                        lambda self, slug, ws: {"disabled_agents": ["verifier"]})
    monkeypatch.setattr(ReviewOrchestrator, "_build_llm_client",
                        lambda self, u, w, p: (client, {}))
    rm = mp.review_models("github", "o/r", workspace_id="ws", user_id="u")
    # verifier is switched off, business_logic is opt-in and nobody opted in
    assert rm.models == ("m-1", "m-2")
    assert dict((m, a) for a, m in rm.targets) == {"m-1": "defect", "m-2": "security"}


def test_a_workspace_that_reviews_with_claude_code_has_no_model_to_ask(monkeypatch):
    import src.api.routers.llm as llm_router

    monkeypatch.setattr(llm_router, "_load_workspace_config",
                        lambda ws: {"review_engine": "claude_code"})
    rm = mp.review_models("github", "o/r", workspace_id="ws", user_id="u")
    assert rm.targets == [] and mp.check_models("ws", rm).available is True


# ─── a review that ended with nothing having answered ────────────────


def test_every_failure_must_be_an_availability_failure_to_wait():
    out = mp.outage_of(["provider_unavailable", "rate_limited"])
    assert isinstance(out, ModelUnavailable) and out.code == "provider_unavailable"
    assert mp.outage_of(["provider_unavailable", "unreadable_reply"]) is None
    assert mp.outage_of(["provider_unavailable", None]) is None
    assert mp.outage_of(["context_too_long"]) is None
    assert mp.outage_of([]) is None
