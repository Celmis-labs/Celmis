"""A second call is only worth making when the first one could have gone better.

The corrective retry next door exists because an unreadable reply is often
fixable: give the model more room and a reminder, and it answers properly. That
logic said nothing about calls that never produced a reply at all, and the
moment a transport retry is added — the ~10 lines this file guards — "it
raised, so try again" starts doing real damage:

  * a rejected API key is rejected again, one PR review later;
  * an unknown model is still unknown;
  * an exhausted quota is still exhausted, and the retry spends a request to
    be told so a second time;
  * a 429 resent inside its own window is worse than a failure — the window it
    lands in is the one it was already too big for. Qodo's pr-agent excludes
    RateLimitError from its retry for exactly this reason.

So the retry decision reads `src.llm.errors.classify`, which the product
already trusts to keep provider payloads away from users, and asks the second
question that taxonomy now answers: would saying this again help?

The counts are the assertions. A terminal failure costs one call, a transient
one costs two, and nothing costs three on the primary — an agent that retried
per failure class would multiply, and the whole point is a ceiling. The one
extension since: a workspace may name a `review_fallback_model`, and that buys
exactly ONE further call, only after a THROTTLED/TRANSIENT death — the second
half of this file counts that too. And the one class added after that:
REFUSED, a 200 that says no, whose second call is never the identical re-ask
— the last section counts where it goes instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import litellm.exceptions as le
import pytest

import src.review.agents.base as base_mod
from src.review.agents.base import AgentContext, LLMReviewAgent
from src.review.models import Hunk, PullRequest
from src.review.settings import AgentLLMSettings

VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'
GARBAGE = "I found issues in [several] places but"  # truncated mid-thought

#: The refusal as it actually arrived — intercepted inside the container on a
#: benchmark run: status 200, no safety flag, no error, prose where the array
#: should be. The same PR answered with a real finding when re-run by hand.
REFUSAL = (
    "Sorry, I cannot fulfill your request to analyze or identify "
    "vulnerabilities in specific code snippets. You can search online for "
    "secure coding guidelines and best practices to help you identify and "
    "address potential security issues in your code."
)

#: A quota refusal as the providers actually phrase it — and as it actually
#: arrived, with a key in the echoed request and the body wrapped around it.
QUOTA_BODY = (
    'litellm.RateLimitError: {"error": {"code": 429, "message": "You exceeded '
    'your current quota, please check your plan and billing details", '
    '"status": "RESOURCE_EXHAUSTED"}, "request": {"api_key": "sk-live-DEADBEEF"}}'
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """The resend now waits before it fires — never on the test clock.

    Patched the way the embedder retry tests patch it: on the `time` module
    the code under test calls, recording what was asked for. The timing
    assertions live in test_a_retry_waits_before_asking_again.py; here the
    patch only keeps the counted calls instant.
    """
    waits: list[float] = []
    monkeypatch.setattr(base_mod.time, "sleep", waits.append)
    return waits


def _response(text: str, model: str = "m") -> MagicMock:
    r = MagicMock()
    r.text = text
    r.input_tokens = 100
    r.output_tokens = 40
    r.cost_usd = 0.01
    r.cost_source = "litellm_estimate"
    r.model = model
    return r


def _ctx(
    outcomes: list, *, fallback_model: str | None = None, model: str = "primary",
) -> tuple[AgentContext, MagicMock]:
    """`outcomes` are replies (str), responses, or exceptions — one per call."""
    client = MagicMock()
    client.generate.side_effect = [
        o if isinstance(o, BaseException | MagicMock) else _response(o)
        for o in outcomes
    ]
    pr = PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@")],
    )
    agent_llm = {}
    if fallback_model is not None:
        agent_llm = {"security": AgentLLMSettings(
            model=model, max_output_tokens=1000, fallback_model=fallback_model,
        )}
    return AgentContext(
        pull_request=pr, llm_client=client, agent_llm=agent_llm,
    ), client


class _Agent(LLMReviewAgent):
    name = "security"
    system_prompt = "find problems"

    def _build_prompt(self, context):
        return "p"


# ─── Hopeless: one call, and a sentence somebody can act on ─────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (le.AuthenticationError(
            message="invalid x-api-key", llm_provider="anthropic", model="claude"),
         "rejected the API key"),
        (le.NotFoundError(
            message="model not found", model="gpt-9", llm_provider="openai"),
         "does not exist at the provider"),
        (le.RateLimitError(
            message=QUOTA_BODY, llm_provider="vertex_ai", model="gemini"),
         "quota exhausted"),
        (le.ContextWindowExceededError(
            message="8000 > 4096", model="gpt-4", llm_provider="openai"),
         "context window"),
    ],
)
def test_a_failure_a_second_call_cannot_fix_is_not_made_twice(exc, expected):
    ctx, client = _ctx([exc, VALID])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 1, "the second call was already answered"
    assert result.error is not None and expected in result.error
    assert result.findings == []


def test_a_rate_limit_is_not_retried_into_the_window_that_refused_it():
    """The one case where the retry is not merely useless but harmful.

    A 429 clears with time; a resend two milliseconds later arrives in the same
    window, is refused again, and on the providers that count refusals it makes
    the next window worse. The run fails now and honestly instead.
    """
    ctx, client = _ctx([
        le.RateLimitError(message="Too many requests", llm_provider="openai",
                          model="gpt-4"),
        VALID,
    ])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 1
    assert result.error is not None and "rate-limiting" in result.error


def test_what_reaches_the_run_record_is_a_reason_not_a_payload():
    """`AgentRunResult.error` is read by people and headed for a summary.

    The quota body that shipped carried the echoed request — key included.
    Nothing from the message may survive; the hint is built from the
    exception's own attributes, which is why the provider and the status do.
    """
    ctx, _ = _ctx([le.RateLimitError(
        message=QUOTA_BODY, llm_provider="vertex_ai", model="gemini")])
    error = _Agent().review(ctx).error

    assert error is not None
    for leak in ("sk-live-DEADBEEF", "RESOURCE_EXHAUSTED", "billing details", "{"):
        assert leak not in error, leak
    assert error.startswith("provider quota exhausted")
    assert "vertex_ai" in error and "429" in error


# ─── Transient: exactly one resend ──────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        le.ServiceUnavailableError(
            message="503", llm_provider="openai", model="gpt-4"),
        le.InternalServerError(
            message="500 internal", llm_provider="openai", model="gpt-4"),
        le.APIConnectionError(
            message="connection reset by peer", llm_provider="openai", model="gpt-4"),
        le.Timeout(message="request timed out", model="gpt-4", llm_provider="openai"),
    ],
)
def test_a_dropped_call_is_resent_once_and_the_review_survives(exc):
    ctx, client = _ctx([exc, VALID])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2
    assert result.error is None
    assert len(result.findings) == 1


def test_the_transport_retry_is_a_resend_not_a_correction():
    """The corrective retry doubles the budget and tells the model its last
    reply was unparseable. Neither applies when there was no reply: the budget
    was never the problem, and the reminder would be a statement about a call
    the model never answered."""
    ctx, client = _ctx([
        le.ServiceUnavailableError(message="503", llm_provider="openai", model="gpt-4"),
        VALID,
    ])
    _Agent().review(ctx)

    first, second = client.generate.call_args_list
    assert second.kwargs["max_output_tokens"] == first.kwargs["max_output_tokens"]
    assert "could not be parsed" not in second.kwargs["system_instruction"]


def test_a_second_dropped_call_is_a_failure_not_a_loop():
    ctx, client = _ctx([
        le.ServiceUnavailableError(message="503", llm_provider="openai", model="gpt-4"),
        le.ServiceUnavailableError(message="503", llm_provider="openai", model="gpt-4"),
        VALID,
    ])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2, "one retry per run, never a ladder"
    assert result.error is not None and "provider is unavailable" in result.error


# ─── The ceiling holds across failure classes ───────────────────────


def test_an_unreadable_reply_still_gets_its_corrective_retry():
    """The behaviour this change had to leave alone."""
    ctx, client = _ctx([GARBAGE, VALID])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2
    assert result.error is None and len(result.findings) == 1
    _, second = client.generate.call_args_list
    assert "could not be parsed" in second.kwargs["system_instruction"]


def test_two_different_failures_still_only_buy_two_calls():
    """One retry per failure CLASS would be two retries here. The budget is per
    run: an unreadable reply spent the second call, so the terminal error that
    answered it is the end of the agent, not the start of a third attempt."""
    ctx, client = _ctx([
        GARBAGE,
        le.AuthenticationError(
            message="invalid x-api-key", llm_provider="anthropic", model="claude"),
        VALID,
    ])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2
    assert result.error is not None and "rejected the API key" in result.error


def test_a_transient_failure_then_an_unreadable_reply_is_not_called_corrective():
    """The run record should not claim a correction that never happened: the
    second call was a resend, so the failure is an unreadable reply, full
    stop."""
    ctx, client = _ctx([
        le.ServiceUnavailableError(message="503", llm_provider="openai", model="gpt-4"),
        GARBAGE,
    ])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2
    assert result.error is not None
    assert result.error.startswith("unreadable reply:")


# ─── The failure mode nobody has met yet ────────────────────────────


def test_an_unclassifiable_failure_behaves_exactly_as_it_did_before():
    """A new provider error must not be quietly absorbed.

    It is not retried — fail-closed, an unknown state refuses — and its own
    text is what lands in the record, because every phrase in the reason table
    was written for a failure somebody understood. Dressing an unrecognised
    error in one of them would hide the day a provider starts raising
    something new.
    """
    ctx, client = _ctx([RuntimeError("proxy 502 from a thing we have not met")])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 1
    assert result.error == "proxy 502 from a thing we have not met"
    assert result.findings == []


# ─── The fallback model: one more call, and only when it could help ──
#
# The workspace may name a `review_fallback_model` (measured motivation: a
# benchmark window where gemini-3.7-flash refused 40% of calls and
# gemini-3.6-flash refused none). It extends the ceiling by exactly one call,
# so the counts below are the budget: throttled = 1 + 1, transient = 2 + 1,
# terminal and unreadable stay what they were.


def test_a_throttled_primary_hands_the_one_extra_call_to_the_fallback_model():
    ctx, client = _ctx(
        [
            le.RateLimitError(message="Too many requests", llm_provider="openai",
                              model="gpt-4"),
            _response(VALID, model="fb"),
        ],
        fallback_model="backup",
    )
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2, "one throttled call, one fallback"
    first, second = client.generate.call_args_list
    assert first.kwargs["model"] == "primary"
    assert second.kwargs["model"] == "backup"
    assert result.error is None and len(result.findings) == 1
    assert result.fallback_used is True
    assert result.model_used == "fb", (
        "the run record must name the model that actually answered"
    )
    assert result.tokens_in == 100, "the fallback reply is on the books"


def test_a_transient_double_death_gets_the_fallback_and_nothing_more():
    """The extended ceiling: two primary calls, one fallback, never a fourth —
    even when the fallback dies of the same outage."""
    boom = le.ServiceUnavailableError(
        message="503", llm_provider="openai", model="gpt-4")
    ctx, client = _ctx([boom, boom, boom, VALID], fallback_model="backup")
    result = _Agent().review(ctx)

    assert client.generate.call_count == 3, "2 primary + 1 fallback is the whole budget"
    assert client.generate.call_args_list[2].kwargs["model"] == "backup"
    assert result.error is not None and "fallback model failed too" in result.error
    assert result.fallback_used is True


def test_a_transient_double_death_is_rescued_by_the_fallback():
    boom = le.ServiceUnavailableError(
        message="503", llm_provider="openai", model="gpt-4")
    ctx, client = _ctx([boom, boom, VALID], fallback_model="backup")
    result = _Agent().review(ctx)

    assert client.generate.call_count == 3
    assert result.error is None and len(result.findings) == 1
    assert result.fallback_used is True


def test_a_terminal_failure_never_reaches_the_fallback():
    """A rejected key fails identically on any model — or, worse, the fallback
    works and masks the configuration mistake until it surfaces as billing."""
    ctx, client = _ctx(
        [le.AuthenticationError(
            message="invalid x-api-key", llm_provider="anthropic", model="claude"),
         VALID],
        fallback_model="backup",
    )
    result = _Agent().review(ctx)

    assert client.generate.call_count == 1
    assert result.error is not None and "rejected the API key" in result.error
    assert result.fallback_used is False


def test_an_exhausted_quota_is_a_429_that_still_never_falls_back():
    """One status code, two dispositions: "slow down" buys the fallback its
    call, "out of quota" is terminal — nobody pays or waits between two lines
    of code, and a fallback answering for it would hide the empty account."""
    ctx, client = _ctx(
        [le.RateLimitError(
            message=QUOTA_BODY, llm_provider="vertex_ai", model="gemini"),
         VALID],
        fallback_model="backup",
    )
    result = _Agent().review(ctx)

    assert client.generate.call_count == 1
    assert result.error is not None and "quota exhausted" in result.error
    assert result.fallback_used is False


def test_an_unclassifiable_failure_never_reaches_the_fallback():
    """Fail-closed holds for the fallback too: an unknown failure is not
    something to answer with more calls."""
    ctx, client = _ctx(
        [RuntimeError("proxy 502 from a thing we have not met"), VALID],
        fallback_model="backup",
    )
    result = _Agent().review(ctx)

    assert client.generate.call_count == 1
    assert result.fallback_used is False


def test_a_parse_failure_never_reaches_the_fallback():
    """The corrective retry is the whole answer to an unreadable reply. A
    different model answering a prompt the primary already answered twice is
    not a fix for a parse failure — it is a comparability leak."""
    ctx, client = _ctx([GARBAGE, GARBAGE], fallback_model="backup")
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2
    assert result.error is not None and "corrective retry" in result.error
    assert result.fallback_used is False


def test_the_fallback_call_is_the_primarys_request_not_a_correction():
    """Same system prompt, same base ceiling: the fallback never answered
    unreadably, so it gets neither the doubled budget nor the reminder. The
    ceiling and reasoning are re-fitted to the fallback model inside
    LLMClient.generate, which is the one place that knows the model."""
    ctx, client = _ctx(
        [le.RateLimitError(message="Too many requests", llm_provider="openai",
                           model="gpt-4"),
         VALID],
        fallback_model="backup",
    )
    _Agent().review(ctx)

    first, second = client.generate.call_args_list
    assert second.kwargs["max_output_tokens"] == first.kwargs["max_output_tokens"]
    assert "could not be parsed" not in second.kwargs["system_instruction"]
    assert second.kwargs["reasoning"] == first.kwargs["reasoning"]


def test_an_unreadable_fallback_reply_is_a_failure_not_a_loop():
    ctx, client = _ctx(
        [le.RateLimitError(message="Too many requests", llm_provider="openai",
                           model="gpt-4"),
         GARBAGE],
        fallback_model="backup",
    )
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2, "one call was the fallback's whole grant"
    assert result.error is not None
    assert result.error.startswith("unreadable reply from the fallback model")
    assert result.fallback_used is True


def test_a_fallback_that_names_the_primary_is_not_spent():
    """Spelled two ways, still one model: retrying the model that just failed
    is a third attempt wearing a different setting, so it is dropped."""
    ctx, client = _ctx(
        [le.RateLimitError(message="Too many requests", llm_provider="openai",
                           model="gemini-3-pro"),
         VALID],
        fallback_model="gemini/gemini-3-pro", model="gemini-3-pro",
    )
    result = _Agent().review(ctx)

    assert client.generate.call_count == 1
    assert result.fallback_used is False


# ─── REFUSED: the second call goes where a different answer is possible ──
#
# Measured: ~11% of security runs ended agent_no_json, and the intercepted
# reply was a refusal — a 200 with prose where the array should be. The
# corrective resend asked again, identically, and got the refusal back word
# for word. So a refusal is never re-asked identically: the slot the
# corrective resend would have taken goes to the fallback model (the case a
# different model exists for), or — when no fallback is configured — to ONE
# re-framed ask of the primary. Either way the total is 2, what an unreadable
# reply costs; never 3 on the primary, and never a fourth call.


def test_a_refusal_is_not_re_asked_identically():
    """No corrective resend: not the doubled budget, not the reminder, and
    not the same question — the identical one was measured to get the
    identical refusal."""
    ctx, client = _ctx([REFUSAL, VALID])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2
    first, second = client.generate.call_args_list
    assert "could not be parsed" not in second.kwargs["system_instruction"], (
        "the corrective reminder is a statement about a reply that was "
        "perfectly readable"
    )
    assert second.kwargs["max_output_tokens"] == first.kwargs["max_output_tokens"], (
        "the refusal was short prose — room was never the problem"
    )
    assert second.kwargs["system_instruction"] != first.kwargs["system_instruction"], (
        "an identical ask was measured to get the identical refusal"
    )
    assert result.error is None and len(result.findings) == 1
    assert result.fallback_used is False


def test_a_refused_primary_hands_its_second_slot_to_the_fallback_model():
    """With a fallback configured the re-framed ask is NOT also made: refused
    → fallback, two calls, and the fallback's answer is the answer."""
    ctx, client = _ctx(
        [REFUSAL, _response(VALID, model="fb")], fallback_model="backup",
    )
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2, "one refused primary, one fallback"
    first, second = client.generate.call_args_list
    assert first.kwargs["model"] == "primary"
    assert second.kwargs["model"] == "backup"
    assert second.kwargs["system_instruction"] == first.kwargs["system_instruction"], (
        "the fallback gets the primary's request — no reminder, no re-framing"
    )
    assert result.error is None and len(result.findings) == 1
    assert result.fallback_used is True
    assert result.model_used == "fb"
    assert result.tokens_in == 200, "the refused reply was paid for too"


def test_two_refusals_and_no_fallback_end_the_agent_naming_the_refusal():
    ctx, client = _ctx([REFUSAL, REFUSAL, VALID])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2, "re-framed once, never a ladder"
    assert result.error is not None
    assert "refused" in result.error
    assert "cannot fulfill your request" in result.error, (
        "the provider's own sentence, not 'unreadable reply'"
    )
    assert "unreadable" not in result.error
    assert result.findings == []
    assert result.fallback_used is False


def test_a_refused_fallback_is_the_end_too():
    ctx, client = _ctx([REFUSAL, REFUSAL, VALID], fallback_model="backup")
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2, "one call was the fallback's whole grant"
    assert result.error is not None and "fallback model refused too" in result.error
    assert result.fallback_used is True


def test_a_refusal_on_the_corrective_call_still_reaches_the_fallback_and_nothing_more():
    """2 primary + 1 fallback is the ceiling however the primary dies. A
    refusal on the second primary call is still a refused primary — the
    fallback is the remedy for that, and the third call is the last."""
    ctx, client = _ctx([GARBAGE, REFUSAL, VALID, VALID], fallback_model="backup")
    result = _Agent().review(ctx)

    assert client.generate.call_count == 3, "2 primary + 1 fallback, never a fourth"
    assert client.generate.call_args_list[2].kwargs["model"] == "backup"
    assert result.error is None and len(result.findings) == 1
    assert result.fallback_used is True


def test_a_refusal_on_the_corrective_call_with_no_fallback_is_not_re_framed():
    """The re-framed ask is the second primary call or nothing: a refusal
    that arrives on the second call finds the primary's budget spent."""
    ctx, client = _ctx([GARBAGE, REFUSAL, VALID])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2
    assert result.error is not None and "refused" in result.error
    assert "cannot fulfill your request" in result.error, (
        "one refusal or two, the record carries the model's own sentence"
    )
    assert result.fallback_used is False
