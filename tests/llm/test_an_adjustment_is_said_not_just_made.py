"""A parameter Celmis changed behind the operator's back is SAID, in one shape.

The runtime already self-heals three ways on one call — a ceiling above the
model max is clamped, a reasoning word the provider refuses is dropped and the
call retried, a temperature the model refuses (claude-sonnet-5 takes only 1)
is dropped and the call retried — and it recorded each somewhere different:
`LLMResult.max_output_tokens_clamped_to`, `LLMResult.reasoning_dropped`, and
for the temperature ONLY the audit record's extra and a log line. The result
the caller held said nothing about the temperature, so no run record could.

What is pinned here, at the client and in the capability memory:

  * every adjustment lands on `LLMResult.parameter_adjustments` as
    {agent, parameter, requested, sent, action, reason, model}, with the
    provider's own sentence as the reason when there is one — the audit
    record carries the same list under the same key;
  * the temperature refusal reaches the result like the reasoning one, is
    remembered with a DATE, is withheld up front on the next call and still
    said there, and is withdrawn when the retry disproves it — the same rules
    `forget_reasoning_refusal` already keeps;
  * `provider_refusals` on the capabilities answer names every learned fact
    with its sentence and its `seen_at`, for a known model and an unknown
    one, and loses a fact the moment it is withdrawn — so /settings/llm can
    say "refused by the provider on <date>" instead of hiding the option.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

#: Known to the installed LiteLLM; its probe offers every effort word and the
#: live API refuses "minimal" — the model the reasoning refusal was measured on.
MODEL = "gemini/gemini-3.7-flash"
REFUSED_WORD = "minimal"
#: Known, with a 4096 output ceiling — the model the clamp was measured on.
SMALL_MODEL = "openai/gpt-4"
#: A self-hosted server LiteLLM has never heard of.
UNKNOWN_MODEL = "openai/celmis-vllm-not-in-any-catalogue"

REASONING_BODY = (
    'litellm.BadRequestError: VertexAIException BadRequestError - {"error": '
    '{"code": 400, "message": "Thinking level MINIMAL is not supported for '
    'this model. Please retry with other thinking level.", "status": '
    '"INVALID_ARGUMENT"}}'
)
REASONING_SENTENCE = (
    "Thinking level MINIMAL is not supported for this model. "
    "Please retry with other thinking level."
)
#: The shape of a temperature refusal as LiteLLM hands it over: its prefix
#: glued to the vendor's JSON, the sentence buried in the middle.
TEMPERATURE_BODY = (
    'litellm.BadRequestError: AnthropicException - {"type":"error","error":'
    '{"type":"invalid_request_error","message":"temperature: only '
    'temperature=1 is supported for this model."}}'
)
TEMPERATURE_SENTENCE = "temperature: only temperature=1 is supported for this model."

VALID = '[{"file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'


# ─── doubles ─────────────────────────────────────────────────────────


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()
        self.model = "m"


class _Provider:
    """`litellm.completion`, refusing what it is told to and answering otherwise.

    `refuse_reasoning` / `refuse_temperature` make it answer 400 — a real
    `litellm.exceptions.BadRequestError` with the real body — to any request
    carrying that parameter. `always` keeps refusing even when the request no
    longer carries it: the disproof case.
    """

    def __init__(self, *, refuse_reasoning: bool = False,
                 refuse_temperature: bool = False, always: bool = False) -> None:
        self.refuse_reasoning = refuse_reasoning
        self.refuse_temperature = refuse_temperature
        self.always = always
        self.kwargs_seen: list[dict] = []

    def __call__(self, **kwargs):
        import litellm

        self.kwargs_seen.append(kwargs)
        if self.refuse_temperature and (self.always or "temperature" in kwargs):
            raise litellm.exceptions.BadRequestError(
                message=TEMPERATURE_BODY, model=kwargs.get("model", MODEL),
                llm_provider="anthropic",
            )
        if self.refuse_reasoning and (self.always or "reasoning_effort" in kwargs):
            raise litellm.exceptions.BadRequestError(
                message=REASONING_BODY, model=kwargs.get("model", MODEL),
                llm_provider="gemini",
            )
        return _Completion(VALID)

    @property
    def temperatures(self) -> list[float | None]:
        return [kw.get("temperature") for kw in self.kwargs_seen]


@pytest.fixture(autouse=True)
def _fresh_capability_state():
    """The refusal memory is process-global on purpose, so it has to be reset."""
    from src.llm.capabilities import reset_capability_caches
    reset_capability_caches()
    yield
    reset_capability_caches()


@pytest.fixture
def provider(monkeypatch):
    def _install(**kw) -> _Provider:
        import litellm
        fake = _Provider(**kw)
        monkeypatch.setattr(litellm, "completion", fake)
        return fake
    return _install


def _client(tmp_path: Path, model: str = MODEL):
    from src.llm.client import LLMClient
    from src.security.audit import AuditLogger

    return LLMClient(
        resolve_key=lambda provider: "sk-test",
        resolve_model=lambda agent: model,
        surface="review",
        audit=AuditLogger(tmp_path / "audit.jsonl"),
        workspace_id="ws-test",
    )


def _generate(client, **kwargs):
    kwargs.setdefault("max_output_tokens", 1024)
    return client.generate(
        prompt="p", agent="architect", operation="review_architect",
        num_retries=0, **kwargs,
    )


def _last_audit_extra(tmp_path: Path) -> dict:
    lines = [ln for ln in (tmp_path / "audit.jsonl").read_text().splitlines() if ln.strip()]
    return json.loads(lines[-1])["extra"]


def _dicts(result) -> list[dict]:
    return [a.as_dict() for a in result.parameter_adjustments]


def _recent(seen_at: str) -> bool:
    stamp = datetime.fromisoformat(seen_at)
    return stamp.tzinfo is not None and datetime.now(UTC) - stamp < timedelta(minutes=5)


# ─── one list, on the result and in the audit record ─────────────────


def test_a_call_that_sent_what_was_asked_carries_no_adjustment(tmp_path, provider):
    provider()

    result = _generate(_client(tmp_path), temperature=0.1, reasoning="high")

    assert result.parameter_adjustments == []
    assert _last_audit_extra(tmp_path)["parameter_adjustments"] == []


def test_a_clamp_is_one_adjustment_naming_both_numbers(tmp_path, provider):
    """requested / sent / the rule — what the scalar field could not say."""
    from src.llm.capabilities import model_capabilities

    fake = provider()
    ceiling = model_capabilities(SMALL_MODEL).max_output_tokens
    assert ceiling, "the clamp model lost its ceiling; pick another"

    result = _generate(_client(tmp_path, SMALL_MODEL), max_output_tokens=ceiling * 4)

    assert fake.kwargs_seen[-1]["max_tokens"] == ceiling
    assert _dicts(result) == [{
        "agent": "architect", "parameter": "max_output_tokens",
        "requested": ceiling * 4, "sent": ceiling, "action": "clamped",
        "reason": f"model ceiling is {ceiling}", "model": SMALL_MODEL,
    }]
    assert _last_audit_extra(tmp_path)["parameter_adjustments"] == _dicts(result), (
        "the audit trail and the result disagree about what was changed"
    )


def test_a_refused_reasoning_level_is_one_adjustment_in_the_providers_words(
    tmp_path, provider,
):
    provider(refuse_reasoning=True)

    result = _generate(_client(tmp_path), reasoning=REFUSED_WORD)

    assert result.text, "the answer without the level is worth more than none"
    assert _dicts(result) == [{
        "agent": "architect", "parameter": "reasoning",
        "requested": REFUSED_WORD, "sent": None, "action": "dropped",
        "reason": REASONING_SENTENCE, "model": MODEL,
    }]


def test_the_remembered_reasoning_refusal_is_still_an_adjustment_next_time(
    tmp_path, provider,
):
    """The second run is where a silent drop hides: nothing fails, nothing
    is retried, the level is simply not sent. The record has to say so then
    too, with the same sentence."""
    provider(refuse_reasoning=True)
    client = _client(tmp_path)
    _generate(client, reasoning=REFUSED_WORD)

    second = _generate(client, reasoning=REFUSED_WORD)

    assert [a.parameter for a in second.parameter_adjustments] == ["reasoning"]
    assert second.parameter_adjustments[0].reason == REASONING_SENTENCE
    assert second.parameter_adjustments[0].requested == REFUSED_WORD


# ─── the temperature: promoted from the audit log to the result ──────


def test_a_refused_temperature_reaches_the_result_not_only_the_audit_log(
    tmp_path, provider,
):
    fake = provider(refuse_temperature=True)

    result = _generate(_client(tmp_path), temperature=0.1)

    assert result.text, "the review died over a temperature"
    assert fake.temperatures == [0.1, None], (
        f"provider calls carried {fake.temperatures} — expected the configured "
        "value once, then the same request without it"
    )
    assert result.temperature_dropped, "the temperature went missing without a word"
    assert "0.1" in result.temperature_dropped
    assert TEMPERATURE_SENTENCE in result.temperature_dropped
    assert _last_audit_extra(tmp_path)["temperature_dropped"] == result.temperature_dropped
    assert _dicts(result) == [{
        "agent": "architect", "parameter": "temperature",
        "requested": 0.1, "sent": None, "action": "dropped",
        "reason": TEMPERATURE_SENTENCE, "model": MODEL,
    }]


def test_the_temperature_sentence_arrives_without_the_json_envelope(tmp_path, provider):
    """What travels to a PR comment is the provider's English, not LiteLLM's
    prefix glued to the vendor's JSON — the cut `reasoning_refusal` already
    makes, now made for the sentence that reaches the same reader."""
    provider(refuse_temperature=True)

    result = _generate(_client(tmp_path), temperature=0.1)

    reason = result.parameter_adjustments[0].reason
    assert reason.startswith("temperature"), reason
    assert "invalid_request_error" not in reason
    assert "{" not in reason and "}" not in reason


def test_the_temperature_refusal_is_remembered_with_a_date(tmp_path, provider):
    from src.llm.capabilities import model_capabilities, provider_refusals

    provider(refuse_temperature=True)
    _generate(_client(tmp_path), temperature=0.1)

    facts = provider_refusals(MODEL)
    assert [(f.parameter, f.value, f.reason) for f in facts] == [
        ("temperature", "0.1", TEMPERATURE_SENTENCE),
    ]
    assert _recent(facts[0].seen_at), facts[0].seen_at
    # And on the capabilities answer, in the wire shape the settings page reads.
    wire = model_capabilities(MODEL).as_dict()["provider_refusals"]
    assert wire == [facts[0].as_dict()]
    assert wire[0]["seen_at"] == facts[0].seen_at


def test_the_next_call_withholds_the_temperature_up_front_and_still_says_so(
    tmp_path, provider,
):
    """Once learned, the value is not paid for again — and not dropped in
    silence either, which is the failure the whole record exists to end."""
    fake = provider(refuse_temperature=True)
    client = _client(tmp_path)
    _generate(client, temperature=0.1)
    calls_before = len(fake.kwargs_seen)

    second = _generate(client, temperature=0.1)

    assert len(fake.kwargs_seen) - calls_before == 1, (
        "the second call paid for the refusal again — the memory is not "
        "consulted before the request is built"
    )
    assert "temperature" not in fake.kwargs_seen[-1]
    assert second.temperature_dropped and TEMPERATURE_SENTENCE in second.temperature_dropped
    assert [(a.parameter, a.requested, a.sent, a.reason)
            for a in second.parameter_adjustments] == [
        ("temperature", 0.1, None, TEMPERATURE_SENTENCE),
    ]
    assert _last_audit_extra(tmp_path)["temperature_dropped"] == second.temperature_dropped


def test_a_different_temperature_is_a_different_fact(tmp_path, provider):
    """The memory holds what was measured, not a theory about the model: 0.1
    refused says nothing about 0.7, which goes out and is measured itself."""
    fake = provider(refuse_temperature=True)
    client = _client(tmp_path)
    _generate(client, temperature=0.1)
    calls_before = len(fake.kwargs_seen)

    _generate(client, temperature=0.7)

    assert fake.temperatures[calls_before] == 0.7, "0.7 was withheld on 0.1's evidence"


def test_a_temperature_refusal_the_retry_disproves_is_withdrawn(tmp_path, provider):
    """The retry without the value is a free controlled experiment. Refused
    identically on a request carrying no temperature, the value was never
    the cause — so the fact is taken back before it makes every later call
    in this process withhold a value the provider never objected to."""
    from src.llm.capabilities import model_capabilities, provider_refusals

    fake = provider(refuse_temperature=True, always=True)

    with pytest.raises(Exception, match="temperature"):
        _generate(_client(tmp_path), temperature=0.1)

    assert len(fake.kwargs_seen) == 2, (
        f"{len(fake.kwargs_seen)} provider calls — one refusal buys exactly "
        "one retry, and a matcher that keeps firing must not keep buying"
    )
    assert provider_refusals(MODEL) == ()
    assert model_capabilities(MODEL).as_dict()["provider_refusals"] == []


def test_a_withdrawn_reasoning_refusal_disappears_from_provider_refusals(
    tmp_path, provider,
):
    """`forget_reasoning_refusal` already put the word back in the dropdown;
    the dated fact has to go with it, or the page would show a refusal the
    vocabulary no longer believes in."""
    from src.llm.capabilities import ReasoningValueRefused, provider_refusals

    provider(refuse_reasoning=True, always=True)

    with pytest.raises(ReasoningValueRefused):
        _generate(_client(tmp_path), reasoning=REFUSED_WORD)

    assert provider_refusals(MODEL) == ()


# ─── the learned facts, where the settings page reads them ───────────


def test_a_known_model_reports_every_learned_fact_with_its_date():
    from src.llm.capabilities import (
        model_capabilities,
        record_reasoning_refusal,
        record_temperature_refusal,
    )

    record_reasoning_refusal(MODEL, {"reasoning_effort": REFUSED_WORD}, REASONING_SENTENCE)
    record_temperature_refusal(MODEL, 0.1, TEMPERATURE_SENTENCE)

    caps = model_capabilities(MODEL)
    assert caps.known is True
    facts = {(f.parameter, f.value): f for f in caps.provider_refusals}
    assert set(facts) == {("reasoning", REFUSED_WORD), ("temperature", "0.1")}
    assert facts[("reasoning", REFUSED_WORD)].reason == REASONING_SENTENCE
    assert facts[("temperature", "0.1")].reason == TEMPERATURE_SENTENCE
    assert all(_recent(f.seen_at) for f in facts.values())
    # The two older lists still agree with the facts.
    assert REFUSED_WORD in (caps.reasoning_values_provider_refused or ())
    assert REFUSED_WORD not in (caps.reasoning_values_router_accepts or ())


def test_an_unknown_model_still_reports_what_the_provider_refused():
    """A self-hosted model has no table entry; its refusals are measured, and
    they are the one thing this build does know about it."""
    from src.llm.capabilities import model_capabilities, record_temperature_refusal

    record_temperature_refusal(UNKNOWN_MODEL, 0.1, TEMPERATURE_SENTENCE)

    caps = model_capabilities(UNKNOWN_MODEL)
    assert caps.known is False
    assert [(f.parameter, f.value) for f in caps.provider_refusals] == [("temperature", "0.1")]
    assert caps.as_dict()["provider_refusals"][0]["reason"] == TEMPERATURE_SENTENCE


def test_two_spellings_of_one_temperature_are_one_fact():
    from src.llm.capabilities import (
        provider_refusals,
        record_temperature_refusal,
        refused_temperature,
    )

    record_temperature_refusal(MODEL, 0.1, TEMPERATURE_SENTENCE)
    record_temperature_refusal(MODEL, 0.10, "a later sentence")

    assert len(provider_refusals(MODEL)) == 1
    assert refused_temperature(MODEL, 0.10) is not None
    assert refused_temperature(MODEL, 0.10).reason == TEMPERATURE_SENTENCE, (
        "the first measurement is the one kept, as for reasoning words"
    )


def test_the_capabilities_endpoint_serves_the_learned_facts(monkeypatch):
    """GET /api/llm/model-capabilities, end to end: the page can say when and
    why, in the provider's words, instead of silently losing a dropdown value."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.deps import current_workspace_id, get_current_user
    from src.api.routers import llm as llm_router
    from src.llm.capabilities import record_reasoning_refusal, record_temperature_refusal

    record_reasoning_refusal(MODEL, {"reasoning_effort": REFUSED_WORD}, REASONING_SENTENCE)
    record_temperature_refusal(MODEL, 0.1, TEMPERATURE_SENTENCE)

    app = FastAPI()
    app.include_router(llm_router.router)
    app.dependency_overrides[get_current_user] = lambda: object()
    app.dependency_overrides[current_workspace_id] = lambda: "default"
    body = TestClient(app).get(
        "/api/llm/model-capabilities", params={"model": MODEL},
    ).json()

    facts = {(f["parameter"], f["value"]): f for f in body["provider_refusals"]}
    assert set(facts) == {("reasoning", REFUSED_WORD), ("temperature", "0.1")}
    assert facts[("reasoning", REFUSED_WORD)]["reason"] == REASONING_SENTENCE
    assert all(_recent(f["seen_at"]) for f in facts.values())
    assert REFUSED_WORD in body["reasoning_values_provider_refused"]
    assert REFUSED_WORD not in body["reasoning_values_router_accepts"]
    # The old key keeps its meaning for the screen that still reads it.
    assert body["reasoning_values"] == body["reasoning_values_router_accepts"]


def test_a_model_nothing_was_learned_about_reports_an_empty_list_not_null():
    from src.llm.capabilities import model_capabilities

    assert model_capabilities(MODEL).as_dict()["provider_refusals"] == []
    assert model_capabilities(UNKNOWN_MODEL).as_dict()["provider_refusals"] == []
