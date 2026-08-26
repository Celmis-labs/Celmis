"""The router accepting a thinking level is not the model accepting it.

GET /api/llm/model-capabilities advertised its reasoning vocabulary by probing
`litellm.get_optional_params` — the function `litellm.completion` calls before
it builds a request. That measures what the ROUTER will send. Measured against
the live Gemini API with a real key, what the MODEL takes is a different set:

    model                     none     minimal  low      medium   high
    gemini-3.7-flash          REFUSED  REFUSED  OK       OK       OK
    gemini-3-flash-preview    OK       OK       OK       OK       OK
    gemini-3.1-flash-lite     OK       OK       OK       OK       OK

    "Thinking level MINIMAL is not supported for this model. Please retry
     with other thinking level."

So the screen whose entire purpose is "what does this model actually take" was
offering two words gemini-3.7-flash answers 400 to, and the operator who picked
one got a failed review and a run record reading "the provider call failed".

What is asserted here is the correction and its blast radius, in that order:

  * a refusal narrows the vocabulary, and only a refusal does — a 429 or a
    rejected key must never delete a working level from everybody's dropdown;
  * the refused call is retried without the level and the answer still arrives,
    because a review that ran without the requested thinking is worth more than
    no review;
  * the level going missing is SAID, on the result and in the audit record, on
    the first run and on every run after it;
  * and the retries do not multiply. The agent's own corrective retry is
    already two calls; the ceiling is pinned at three, once, by a test, so that
    the next person to add a retry has to change a number somebody wrote down.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

#: LiteLLM has an entry for this and its probe offers all five effort words.
#: The live API refuses two of them — this is the model the defect was measured
#: on, so it is the model the fix is measured on.
MODEL = "gemini/gemini-3.7-flash"
#: One of the two the provider refuses.
REFUSED = "minimal"
#: One it takes. Nothing here may narrow this one.
ACCEPTED = "high"

#: What LiteLLM hands us when Google refuses the level: its own prefix glued to
#: Google's JSON body. The sentence we want is buried in the middle of it, and
#: the envelope is exactly what src/llm/errors.py exists to keep off a screen.
REFUSAL_BODY = (
    'litellm.BadRequestError: VertexAIException BadRequestError - {"error": '
    '{"code": 400, "message": "Thinking level MINIMAL is not supported for '
    'this model. Please retry with other thinking level.", "status": '
    '"INVALID_ARGUMENT"}}'
)
#: The English inside it — what an operator should be shown, and nothing else.
REFUSAL_SENTENCE = (
    "Thinking level MINIMAL is not supported for this model. "
    "Please retry with other thinking level."
)

VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'


# ─── doubles ─────────────────────────────────────────────────────────


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)
        self.finish_reason = "stop"


class _Usage:
    prompt_tokens = 100
    completion_tokens = 40
    prompt_tokens_details = None
    total_cost = 0.01


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _RefusesTheLevel:
    """A provider that 400s any request carrying `reasoning_effort`.

    Which is the whole behaviour under test: the parameter is what it objects
    to, so a request without it succeeds and a request with it does not. The
    error is a real `litellm.exceptions.BadRequestError` carrying the real
    body — a hand-rolled stand-in would let a matcher that only works on a
    tidy string pass here and fail in production.
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.kwargs_seen: list[dict] = []

    def __call__(self, **kwargs):
        import litellm

        self.kwargs_seen.append(kwargs)
        if "reasoning_effort" in kwargs:
            raise litellm.exceptions.BadRequestError(
                message=REFUSAL_BODY, model=kwargs.get("model", MODEL),
                llm_provider="gemini",
            )
        return _Completion(self.replies.pop(0) if self.replies else VALID)

    @property
    def efforts(self) -> list[str | None]:
        return [kw.get("reasoning_effort") for kw in self.kwargs_seen]


class _RaisesEveryTime:
    """A provider that fails identically whatever the request carried."""

    def __init__(self, exc_factory) -> None:
        self._exc_factory = exc_factory
        self.kwargs_seen: list[dict] = []

    def __call__(self, **kwargs):
        self.kwargs_seen.append(kwargs)
        raise self._exc_factory(kwargs.get("model", MODEL))


def _quota(model: str):
    """A 429 whose body names a thinking level AND says it is unavailable.

    Deliberately worded so that the message-matching alone would take it for a
    refusal: a provider that meters thinking separately has every reason to
    write a quota error like this. The 400-only condition is the one thing
    standing between it and a working effort word deleted process-wide.
    """
    import litellm

    return litellm.exceptions.RateLimitError(
        message=(
            '{"error": {"code": 429, "message": "Quota exceeded: thinking '
            'level high is not available on your current plan.", "status": '
            '"RESOURCE_EXHAUSTED"}}'
        ),
        model=model, llm_provider="gemini",
    )


def _rejected_key(model: str):
    """A 401 whose body says "not valid" and never names the parameter.

    Guards the other half of the matcher: a refusal has to NAME the reasoning
    parameter, or every rejected key on a thinking-enabled agent would strike
    that agent's level off the list.
    """
    import litellm

    return litellm.exceptions.AuthenticationError(
        message="API key not valid. Please pass a valid API key.",
        model=model, llm_provider="gemini",
    )


def _unrelated_bad_request(model: str):
    """A 400 about something else, on a call that did carry a thinking level.

    This is the case the status check cannot catch — same status, same
    request — so it is the one that leans entirely on the matcher requiring
    the provider to NAME the reasoning parameter. A malformed generation
    config is a 400 saying "Invalid value" about a field that is not ours, and
    treating it as a verdict on the thinking level would strike a working word
    off the list for the rest of the process's life.
    """
    import litellm

    return litellm.exceptions.BadRequestError(
        message=(
            'litellm.BadRequestError: VertexAIException BadRequestError - '
            '{"error": {"code": 400, "message": "Invalid value at '
            "'generation_config.temperature' (must be between 0 and 2).\", "
            '"status": "INVALID_ARGUMENT"}}'
        ),
        model=model, llm_provider="gemini",
    )


def _refuses_forever(model: str):
    import litellm

    return litellm.exceptions.BadRequestError(
        message=REFUSAL_BODY, model=model, llm_provider="gemini",
    )


# ─── fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_capability_state():
    """The refusal memory is process-global on purpose, so it has to be reset.

    Without this, the first test to record a refusal narrows the vocabulary for
    every test after it in the same process — and the ones asserting that a
    working level survives would pass or fail on ordering.
    """
    from src.llm.capabilities import reset_capability_caches
    reset_capability_caches()
    yield
    reset_capability_caches()


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
    return client.generate(
        prompt="p", agent="architect", operation="review_architect",
        max_output_tokens=1024, num_retries=0, **kwargs,
    )


def _audit_records(tmp_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _offered(model: str = MODEL) -> tuple[str, ...]:
    from src.llm.capabilities import model_capabilities

    return model_capabilities(model).reasoning_values_router_accepts or ()


# ─── the vocabulary narrows, and only a refusal narrows it ───────────


def test_the_probe_offers_a_word_the_provider_refuses(monkeypatch):
    """The defect itself. Without this the fix below could be a no-op.

    Asked of the installed LiteLLM, unmocked: `minimal` survives the function
    `litellm.completion` calls before building a request, which is precisely
    why it reached a dropdown and precisely why that was never evidence.
    """
    import litellm
    from litellm.utils import get_optional_params

    bare, provider = litellm.get_llm_provider(model=MODEL)[:2]
    translated = get_optional_params(
        model=bare, custom_llm_provider=provider, reasoning_effort=REFUSED,
    )

    assert "thinkingConfig" in translated, (
        "LiteLLM stopped translating 'minimal' for this model — if the router "
        "now refuses it too, this whole module is measuring a gap that closed"
    )
    assert REFUSED in _offered(), (
        "the probe no longer offers the word the provider refuses; the rest of "
        "this file is asserting a correction to a defect that is gone"
    )


def test_a_refusal_takes_the_word_out_of_what_this_build_offers(
    tmp_path, monkeypatch,
):
    import litellm

    fake = _RefusesTheLevel()
    monkeypatch.setattr(litellm, "completion", fake)

    _generate(_client(tmp_path), reasoning=REFUSED)

    caps = _model_caps()
    assert REFUSED not in (caps.reasoning_values_router_accepts or ()), (
        f"the provider refused {REFUSED!r} and the screen still offers "
        f"{caps.reasoning_values_router_accepts} — the whole point is that the "
        "provider outranks the router"
    )
    assert REFUSED in (caps.reasoning_values_provider_refused or ()), (
        "the word vanished without the screen being able to say why it went"
    )
    assert ACCEPTED in (caps.reasoning_values_router_accepts or ()), (
        "one refusal emptied the dropdown; a refusal is about ONE word"
    )


def test_the_wire_shape_names_the_authority_that_answered_it():
    """The API field said "supports"; nothing here had asked the model.

    The name is the contract — a screen that reads `reasoning_values` and
    labels it "what this model supports" is repeating a claim the probe cannot
    make. So the key says whose answer it is, and the refusals travel beside it
    rather than being silently subtracted.
    """
    from src.llm.capabilities import model_capabilities

    caps = model_capabilities(MODEL).as_dict()

    assert "reasoning_values_router_accepts" in caps
    assert "reasoning_values_provider_refused" in caps
    assert caps["reasoning_values_provider_refused"] is None, (
        "nothing has been refused yet, and an empty list would read as "
        "'we asked and the answer was none'"
    )


def test_the_two_lists_partition_what_the_router_offered(tmp_path, monkeypatch):
    """The refused word leaves the offered list and appears in the other one.

    Asserted together because the failure that matters is a word that falls
    out of both — dropped from the dropdown with no record of where it went.
    """
    import litellm

    probed = set(_probe())
    monkeypatch.setattr(litellm, "completion", _RefusesTheLevel())

    _generate(_client(tmp_path), reasoning=REFUSED)

    caps = _model_caps()
    offered = set(caps.reasoning_values_router_accepts or ())
    refused = set(caps.reasoning_values_provider_refused or ())
    assert offered | refused == probed
    assert not (offered & refused)


@pytest.mark.parametrize(
    ("name", "factory"),
    [
        ("a 429 out of quota", _quota),
        ("a rejected key", _rejected_key),
        ("a 400 about another field", _unrelated_bad_request),
    ],
)
def test_a_failure_that_is_not_about_the_level_leaves_it_alone(
    tmp_path, monkeypatch, name, factory,
):
    """The matcher must be about the shape of a reasoning refusal, not about
    "the call failed while we were sending reasoning".

    All three arrive on a request carrying `reasoning_effort`, and each one
    reaches a different condition: the quota body names a thinking level and
    says it is unavailable, and is stopped by the status; the rejected key says
    "not valid" and is stopped by never naming the parameter; the unrelated 400
    has the right status and the refusal words and is stopped by the same. Any
    of them allowed through would delete a working level from every workspace's
    dropdown, process-wide, because of something that is not about the level.
    """
    import litellm

    fake = _RaisesEveryTime(factory)
    monkeypatch.setattr(litellm, "completion", fake)

    with pytest.raises(Exception) as caught:
        _generate(_client(tmp_path), reasoning=ACCEPTED)

    assert type(caught.value) is type(factory(MODEL)), (
        f"{name} was swallowed or rewritten; it has to reach the caller as "
        "itself so the run record can say quota, not 'reasoning'"
    )
    assert len(fake.kwargs_seen) == 1, (
        f"{name} bought a second provider call it can never benefit from"
    )
    assert set(_offered()) == set(_probe()), (
        f"{name} narrowed the vocabulary — nothing about it is evidence "
        "about which words this model takes"
    )
    assert ACCEPTED in _offered(), (
        f"{name} deleted a working effort word from the vocabulary"
    )


# ─── the call still gets an answer ───────────────────────────────────


def test_a_reasoning_shaped_400_on_a_call_that_asked_for_none_is_not_ours(
    tmp_path, monkeypatch,
):
    """The condition no message-matching can substitute for.

    A call that sent no reasoning parameter cannot have been refused for one,
    whatever the body says — and a body CAN say it: a provider echoing a
    default thinkingConfig back inside a complaint about something else would
    otherwise delete a word nobody had even asked for. There is also nothing
    to retry without, so a retry here would be the same failing request sent
    twice.
    """
    import litellm

    fake = _RaisesEveryTime(_refuses_forever)
    monkeypatch.setattr(litellm, "completion", fake)

    with pytest.raises(litellm.exceptions.BadRequestError):
        _generate(_client(tmp_path))

    assert len(fake.kwargs_seen) == 1, (
        "a request that carried no thinking level bought a retry without one"
    )
    assert set(_offered()) == set(_probe()), (
        "a 400 poisoned the vocabulary for a level this call never sent"
    )


def test_the_refused_level_is_dropped_and_the_call_retried_once(
    tmp_path, monkeypatch,
):
    import litellm

    fake = _RefusesTheLevel(replies=["the answer"])
    monkeypatch.setattr(litellm, "completion", fake)

    result = _generate(_client(tmp_path), reasoning=REFUSED)

    assert result.text == "the answer", (
        "the review died over a thinking level; the answer without it is worth "
        "more than no answer"
    )
    assert fake.efforts == [REFUSED, None], (
        f"provider calls carried {fake.efforts} — expected the configured "
        "level once, then the same request without it"
    )


def test_the_dropped_level_is_recorded_in_the_providers_own_words(
    tmp_path, monkeypatch,
):
    """A silent recovery is the bug wearing a different hat.

    `gemini_thinking_budget` spent a release reaching nothing and saying
    nothing about it. A review that quietly ran without the thinking level
    somebody configured looks exactly like one that ran with it, so the result
    and the audit record both have to carry the reason — and the reason is the
    provider's sentence, which is the clearest one anybody will be handed.
    """
    import litellm

    monkeypatch.setattr(litellm, "completion", _RefusesTheLevel())

    result = _generate(_client(tmp_path), reasoning=REFUSED)

    assert result.reasoning_dropped, "the level went missing without a word"
    assert REFUSAL_SENTENCE in result.reasoning_dropped, (
        f"the note reads {result.reasoning_dropped!r} — the provider's own "
        "sentence is the whole reason this string exists"
    )
    assert REFUSED in result.reasoning_dropped, "the note does not name the level"
    assert "INVALID_ARGUMENT" not in result.reasoning_dropped, (
        "the JSON envelope came along; src/llm/errors.py exists to keep "
        "provider bodies off screens and this is a provider body"
    )

    extra = _audit_records(tmp_path)[-1]["extra"]
    assert extra["reasoning_dropped"] == result.reasoning_dropped


def test_every_later_call_still_says_the_level_is_not_being_sent(
    tmp_path, monkeypatch,
):
    """The second run is where a silent drop would hide.

    Once the pair is remembered the parameter is never sent again, so there is
    no failure to notice and nothing to report — which is exactly how a setting
    that reaches nothing survives for a release.
    """
    import litellm

    fake = _RefusesTheLevel()
    monkeypatch.setattr(litellm, "completion", fake)
    client = _client(tmp_path)

    _generate(client, reasoning=REFUSED)
    calls_after_first = len(fake.kwargs_seen)
    second = _generate(client, reasoning=REFUSED)

    assert len(fake.kwargs_seen) - calls_after_first == 1, (
        "the second call paid for the refusal again — the memory is not being "
        "consulted before the request is built"
    )
    assert fake.efforts[-1] is None
    assert second.reasoning_dropped and REFUSAL_SENTENCE in second.reasoning_dropped, (
        "the level was skipped in silence on the run after the first one"
    )


def test_a_refusal_that_survives_the_retry_carries_the_providers_sentence(
    tmp_path, monkeypatch,
):
    """The retry is not a loop, and what comes out of it is not generic.

    A 400 that keeps arriving on a request carrying no reasoning parameter
    means the matcher fired on something it should not have. Stopping is the
    fail-closed answer; what travels is the provider's sentence, because the
    alternative is `classify`'s "the provider call failed", which is true and
    useless about a failure the provider spelled out.
    """
    import litellm

    from src.llm.capabilities import ReasoningValueRefused

    fake = _RaisesEveryTime(_refuses_forever)
    monkeypatch.setattr(litellm, "completion", fake)

    with pytest.raises(ReasoningValueRefused) as caught:
        _generate(_client(tmp_path), reasoning=REFUSED)

    assert str(caught.value) == REFUSAL_SENTENCE
    assert len(fake.kwargs_seen) == 2, (
        f"{len(fake.kwargs_seen)} provider calls — one refusal buys exactly "
        "one retry, and a matcher that keeps firing must not keep buying"
    )
    assert set(_offered()) == set(_probe()), (
        "the word stayed struck off after the retry disproved the reason for "
        "striking it: the second call carried no thinking level and was "
        "refused identically, so the level was never what the provider "
        "objected to — and a value missing from every workspace's dropdown on "
        "a mismatched 400 is the failure this matcher is most afraid of"
    )


def test_that_sentence_survives_into_a_run_record_verbatim(tmp_path, monkeypatch):
    """The claim above is only worth something if nothing downstream rewrites it.

    `errors.classify` reports an UNRECOGNISED failure as `str(exc)` precisely
    so a failure it cannot name is not dressed up as one it can, and that is
    the path this exception takes to `AgentRunResult.error`.
    """
    from src.llm.capabilities import ReasoningValueRefused
    from src.llm.errors import classify
    from src.review.agents.base import _agent_error_text

    exc = ReasoningValueRefused(REFUSAL_SENTENCE)

    assert _agent_error_text(exc, classify(exc)) == REFUSAL_SENTENCE, (
        "the clearest sentence anybody will get about this failure was "
        "replaced with a generic one on the way to the run record"
    )


# ─── the retries do not multiply ─────────────────────────────────────


def _agent_and_context(llm_client, agent_llm):
    from src.review.agents.base import AgentContext, LLMReviewAgent
    from src.review.models import Hunk, PullRequest

    class _Agent(LLMReviewAgent):
        name = "architect"
        system_prompt = "find problems"

        def _build_prompt(self, context):
            return "p"

    pr = PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@")],
    )
    return _Agent(), AgentContext(
        pull_request=pr, llm_client=llm_client, agent_llm=agent_llm,
    )


def _architect(tmp_path, reasoning):
    from src.review.settings import AgentLLMSettings

    return _agent_and_context(
        _client(tmp_path),
        {"architect": AgentLLMSettings(
            model=MODEL, max_output_tokens=1024, reasoning=reasoning,
        )},
    )


def test_one_agent_never_makes_more_than_three_provider_calls(
    tmp_path, monkeypatch,
):
    """The worst case, pinned, because retries compose and nobody counted.

    Two retries now exist on the same call path and neither knows about the
    other: `LLMReviewAgent._generate_and_parse` spends a second call correcting
    an unreadable reply, and `LLMClient.generate` spends one re-sending without
    a refused thinking level. This is both of them firing in the same agent run
    — a refusal on the first call AND an unparseable reply after it — which is
    the most calls the pair can produce:

        1. configured level → the provider refuses it
        2. same request, no level → an unreadable reply
        3. corrective retry — no level, because the pair is now remembered

    Four would mean the corrective retry re-sent the level the provider had
    already refused; five would mean the drop-and-retry had started nesting.
    """
    import litellm

    fake = _RefusesTheLevel(replies=["not json at all", VALID])
    monkeypatch.setattr(litellm, "completion", fake)
    agent, ctx = _architect(tmp_path, REFUSED)

    result = agent.review(ctx)

    assert len(fake.kwargs_seen) == 3, (
        f"the agent made {len(fake.kwargs_seen)} provider calls carrying "
        f"{fake.efforts}; the documented ceiling is three"
    )
    assert fake.efforts == [REFUSED, None, None], (
        f"calls carried {fake.efforts} — after the refusal the level must "
        "never be sent again, and the corrective retry is where that would slip"
    )
    assert len(result.findings) == 1, (
        "the run failed over a thinking level, which is what the retry exists "
        "to prevent"
    )


def test_the_standing_cost_is_still_two_calls_once_the_refusal_is_known(
    tmp_path, monkeypatch,
):
    """Three is a first-encounter price, not a new normal.

    If it were standing, every agent of every review on this model would pay a
    rejected call forever, and the process-local memory this was built on would
    be buying nothing.
    """
    import litellm

    fake = _RefusesTheLevel(replies=["not json at all", VALID, VALID])
    monkeypatch.setattr(litellm, "completion", fake)
    agent, ctx = _architect(tmp_path, REFUSED)
    agent.review(ctx)
    learned = len(fake.kwargs_seen)

    fake.replies = ["not json at all", VALID]
    agent, ctx = _architect(tmp_path, REFUSED)
    agent.review(ctx)

    assert len(fake.kwargs_seen) - learned == 2, (
        "a second review on the same model paid the refusal again"
    )


def test_an_agent_whose_level_is_accepted_still_makes_two(tmp_path, monkeypatch):
    """The unchanged path. A retry added for one failure must not become a
    tax on the calls that never hit it."""
    import litellm

    fake = _RefusesTheLevel(replies=["not json at all", VALID])
    monkeypatch.setattr(litellm, "completion", fake)
    agent, ctx = _architect(tmp_path, None)

    agent.review(ctx)

    assert len(fake.kwargs_seen) == 2
    assert fake.efforts == [None, None]


def _model_caps():
    from src.llm.capabilities import model_capabilities

    return model_capabilities(MODEL)


def _probe() -> tuple[str, ...]:
    """What the ROUTER offers, untouched by any refusal — the raw probe."""
    from src.llm.capabilities import _effort_vocabulary

    return _effort_vocabulary(MODEL)
