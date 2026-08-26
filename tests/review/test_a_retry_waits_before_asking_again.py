"""The retry's WHEN: a resend fires after a pause, never in the same millisecond.

The transport resend used to be a bare `continue`. Measured against a real
fork set, ConnectError was 5 of the 9 agent failures in one run — and most of
those were the second attempt dying on the same dead socket it had just died
on, because a dropped connection does not heal in a millisecond.

So the one call that follows a failure now waits first: ~2s for TRANSIENT,
longer for THROTTLED — where the provider explicitly said wait, and its own
Retry-After outranks our default when the exception carries one. The
corrective resend waits for nothing: the reply arrived fine, the content was
the problem, and waiting buys nothing.

Backoff changes WHEN the calls happen, never HOW MANY — the counts stay pinned
in test_a_hopeless_call_is_not_retried.py. The clock is patched the way the
embedder retry tests patch it; nothing here ever really sleeps.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import litellm.exceptions as le
import pytest

import src.review.agents.base as base_mod
from src.review.agents.base import AgentContext, LLMReviewAgent
from src.review.models import Hunk, PullRequest
from src.review.settings import AgentLLMSettings

VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'
GARBAGE = "I found issues in [several] places but"  # truncated mid-thought

TRANSIENT_503 = le.ServiceUnavailableError(
    message="503", llm_provider="openai", model="gpt-4")
THROTTLED_429 = le.RateLimitError(
    message="Too many requests", llm_provider="openai", model="gpt-4")


def _rate_limit_with_retry_after(value: str, *, where: str) -> le.RateLimitError:
    """A 429 carrying Retry-After where the installed litellm (1.97.0)
    actually puts one — measured, not assumed: there is no `retry_after`
    attribute. `exc.headers` is the dict the LiteLLM proxy attaches;
    `exc.response.headers` is where a vendor's own 429 headers survive."""
    if where == "proxy_dict":
        return le.RateLimitError(
            message="Too many requests", llm_provider="openai", model="gpt-4",
            headers={"Retry-After": value},
        )
    response = httpx.Response(
        429, headers={"Retry-After": value},
        request=httpx.Request("POST", "https://api.example.test"),
    )
    return le.RateLimitError(
        message="Too many requests", llm_provider="openai", model="gpt-4",
        response=response,
    )


@pytest.fixture()
def clock(monkeypatch):
    """Records what the code asked the clock for, sleeping never."""
    waits: list[float] = []
    monkeypatch.setattr(base_mod.time, "sleep", waits.append)
    return waits


def _response(text: str) -> MagicMock:
    r = MagicMock()
    r.text = text
    r.input_tokens = 100
    r.output_tokens = 40
    r.cost_usd = 0.01
    r.cost_source = "litellm_estimate"
    r.model = "m"
    return r


def _ctx(
    outcomes: list, *, fallback_model: str | None = None,
) -> tuple[AgentContext, MagicMock]:
    client = MagicMock()
    client.generate.side_effect = [
        o if isinstance(o, BaseException) else _response(o) for o in outcomes
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
            model="primary", max_output_tokens=1000, fallback_model=fallback_model,
        )}
    return AgentContext(
        pull_request=pr, llm_client=client, agent_llm=agent_llm,
    ), client


class _Agent(LLMReviewAgent):
    name = "security"
    system_prompt = "find problems"

    def _build_prompt(self, context):
        return "p"


# ─── TRANSIENT: ~2s, and BEFORE the resend, not after ───────────────


def test_the_transport_resend_waits_two_seconds_first(monkeypatch):
    events: list[object] = []
    outcomes = [TRANSIENT_503, _response(VALID)]

    def _generate(**kwargs):
        events.append("call")
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    ctx, client = _ctx([])
    client.generate = MagicMock(side_effect=_generate)
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: events.append(("wait", s)))
    result = _Agent().review(ctx)

    assert result.error is None
    assert events == ["call", ("wait", 2.0), "call"], (
        "the pause belongs BETWEEN the failure and the resend — a sleep after "
        "the resend, or none at all, is the millisecond retry this file "
        "exists to end"
    )


def test_the_transient_pause_is_the_stated_constant(clock):
    ctx, _ = _ctx([TRANSIENT_503, VALID])
    _Agent().review(ctx)

    assert clock == [_Agent._TRANSIENT_BACKOFF_SECONDS]
    assert clock == [pytest.approx(2.0)]


# ─── Corrective: no pause at all ────────────────────────────────────


def test_the_corrective_resend_never_waits(clock):
    """The reply arrived fine; the content was the problem. Waiting buys
    nothing, and the review budget is 300s for everything."""
    ctx, client = _ctx([GARBAGE, VALID])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2
    assert result.error is None
    assert clock == []


# ─── THROTTLED: longer, Retry-After first, and only before a call ───


def test_a_throttled_death_with_no_fallback_waits_for_nothing(clock):
    """No call follows, so no pause: sleeping on the way to an error record
    would hold a worker thread to make a dead agent look slow."""
    ctx, client = _ctx([THROTTLED_429, VALID])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 1
    assert result.error is not None
    assert clock == []


def test_the_fallback_after_a_429_waits_longer_than_a_transport_blip(clock):
    ctx, _ = _ctx([THROTTLED_429, VALID], fallback_model="backup")
    result = _Agent().review(ctx)

    assert result.error is None
    assert clock == [_Agent._THROTTLED_BACKOFF_SECONDS]
    assert clock[0] > _Agent._TRANSIENT_BACKOFF_SECONDS, (
        "the provider explicitly said wait — the default pause has to be "
        "longer than the one for a connection blip"
    )


@pytest.mark.parametrize("where", ["proxy_dict", "vendor_response"])
def test_the_providers_own_retry_after_outranks_the_default(clock, where):
    ctx, _ = _ctx(
        [_rate_limit_with_retry_after("7", where=where), VALID],
        fallback_model="backup",
    )
    result = _Agent().review(ctx)

    assert result.error is None
    assert clock == [pytest.approx(7.0)]


def test_an_absurd_retry_after_is_capped_not_obeyed(clock):
    """A provider that says 3600 is asking for more than the whole review
    budget. Past the cap the honest move is to make the one remaining call
    and let it fail, not to hold a thread for an hour."""
    ctx, _ = _ctx(
        [_rate_limit_with_retry_after("3600", where="vendor_response"), VALID],
        fallback_model="backup",
    )
    _Agent().review(ctx)

    assert clock == [_Agent._MAX_RETRY_AFTER_SECONDS]
    assert clock[0] <= 30.0


def test_an_unreadable_retry_after_falls_back_to_the_default(clock):
    """The HTTP-date form is legal but unseen from LLM providers, and a
    misparsed date becomes a real sleep — so anything non-numeric means the
    default, not a guess."""
    ctx, _ = _ctx(
        [_rate_limit_with_retry_after(
            "Wed, 21 Oct 2026 07:28:00 GMT", where="vendor_response"), VALID],
        fallback_model="backup",
    )
    _Agent().review(ctx)

    assert clock == [_Agent._THROTTLED_BACKOFF_SECONDS]


def test_a_transient_double_death_waits_before_the_fallback_too(clock):
    ctx, _ = _ctx(
        [TRANSIENT_503, TRANSIENT_503, VALID], fallback_model="backup",
    )
    result = _Agent().review(ctx)

    assert result.error is None
    assert clock == [
        _Agent._TRANSIENT_BACKOFF_SECONDS, _Agent._TRANSIENT_BACKOFF_SECONDS,
    ], "one pause before the resend, one before the fallback — never zero"
