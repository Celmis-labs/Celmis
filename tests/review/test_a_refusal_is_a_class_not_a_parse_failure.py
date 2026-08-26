"""A polite no is not a truncated array, and does not get the truncated
array's remedy.

Measured on a benchmark run against real forks: the security agent failed
~11% of runs with `agent_no_json`, on the first attempt and on the corrective
one. Intercepting the call showed a 200 carrying 248 characters of prose —

    Sorry, I cannot fulfill your request to analyze or identify
    vulnerabilities in specific code snippets. You can search online for
    secure coding guidelines…

— no JSON, no safety flag, no error status. The same PR re-run by hand
answered with a real finding, so the refusal is weather, not a property of
the input. Filed under "unreadable", it got the corrective resend: the same
question, identically, with twice the room and a reminder to emit JSON. It
got the same refusal back word for word, and the fallback model — the one
thing that could have helped — never engaged, because refusals were not a
class the ladder knew.

Pinned here: the matcher that tells a refusal from a truncated array (narrow,
with its negatives), the exception it raises (still an AgentReplyUnreadable,
so nothing that catches the parent can miss it), what the second call looks
like when it is re-framed rather than corrected, and what the run record
says. The CALL COUNTS live in test_a_hopeless_call_is_not_retried.py; this
file is about the shape of the calls, not their number.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import src.review.agents.base as base_mod
from src.review.agents.base import (
    AgentContext,
    AgentRefused,
    AgentReplyUnreadable,
    LLMReviewAgent,
    looks_like_refusal,
)
from src.review.models import Hunk, PullRequest
from src.review.settings import AgentLLMSettings

VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "critical", "title": "t", "body": "b"}]'
GARBAGE = "I found issues in [several] places but"  # truncated mid-thought

#: The refusal as it actually arrived, tail as recorded.
REAL_REFUSAL = (
    "Sorry, I cannot fulfill your request to analyze or identify "
    "vulnerabilities in specific code snippets. You can search online for "
    "secure coding guidelines and best practices to help you identify and "
    "address potential security issues in your code."
)

#: A findings array cut off by the output budget — the corrective resend's
#: whole reason to exist, and the one thing the matcher must never call a
#: refusal: a refusal gets no budget doubling.
TRUNCATED_ARRAY = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "warning", "title": "t", "body": "the'

#: A real finding whose body happens to carry every refusal word there is.
FINDING_THAT_SAYS_CANNOT = (
    '[{"reasoning": "None reaches line 3 and is dereferenced there", '
    '"file": "a.py", "line": 3, "severity": "warning", "title": "None not handled", '
    '"body": "Sorry to say the function cannot help callers that pass None; '
    'I am unable to see a guard, and it will not assist the retry path."}]'
)


@pytest.fixture()
def clock(monkeypatch):
    """Records what the code asked the clock for, sleeping never."""
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
    outcomes: list, *, fallback_model: str | None = None,
) -> tuple[AgentContext, MagicMock]:
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
        agent_llm = {"probe": AgentLLMSettings(
            model="primary", max_output_tokens=1000, fallback_model=fallback_model,
        )}
    return AgentContext(
        pull_request=pr, llm_client=client, agent_llm=agent_llm,
    ), client


class _Agent(LLMReviewAgent):
    # Not "security": that name resolves to the canonical security prompt via
    # _compose_effective_system_prompt, and that prompt now carries the
    # authorising frame from its first line (see security.py — 20% refusals
    # per call made it the cheaper fix). These tests are about the LADDER —
    # the re-frame must be the second ask's difference — so they run on an
    # agent whose base prompt says nothing of the kind.
    name = "probe"
    system_prompt = "find problems"

    def _build_prompt(self, context):
        return "p"


# ─── The matcher: the real refusal, and the things that are not one ──


@pytest.mark.parametrize("reply", [
    pytest.param(REAL_REFUSAL, id="the measured refusal"),
    pytest.param("I can't help with that.", id="bare can't-help"),
    pytest.param("I'm sorry, but I'm not able to assist with this request.",
                 id="apology + not able + assist"),
    pytest.param("This request violates our content policy.", id="policy sentence"),
    pytest.param("Unfortunately, I won't be able to review this change as it goes "
                 "against my guidelines.", id="regret + won't + review"),
    pytest.param("<think>is this allowed? see [policy 1]</think>\nSorry, I cannot "
                 "help with analyzing this code.", id="refusal after a reasoning trace"),
])
def test_a_refusal_is_recognised(reply):
    assert looks_like_refusal(reply) is True


@pytest.mark.parametrize("reply", [
    pytest.param(TRUNCATED_ARRAY, id="truncated JSON array"),
    pytest.param(FINDING_THAT_SAYS_CANNOT, id="'cannot' inside a finding body"),
    pytest.param("", id="empty reply"),
    pytest.param("   \n ", id="whitespace reply"),
    pytest.param(GARBAGE, id="prose cut mid-thought"),
    pytest.param("I could not review this.", id="a shrug is not a refusal shape"),
    pytest.param("Here is my analysis of the change. " * 20 + "Sorry, I cannot help "
                 "further.", id="an essay with an apology at the end"),
    pytest.param('{"error": "Sorry, I cannot help with that"}', id="refusal in JSON clothing"),
])
def test_what_is_not_a_refusal_is_not_called_one(reply):
    """Every one of these stays plain-unreadable (or readable), which means
    the corrective resend — more room, a reminder — is still what it gets.
    The finding body is the sharp one: every refusal word, inside brackets,
    and the matcher must read the brackets first."""
    assert looks_like_refusal(reply) is False


# ─── The exception: a refusal, and still an unreadable reply ─────────


def test_the_measured_refusal_raises_the_refusal_class():
    with pytest.raises(AgentRefused) as caught:
        _Agent()._parse_findings(REAL_REFUSAL, None)
    assert isinstance(caught.value, AgentReplyUnreadable), (
        "anything that catches the parent must keep catching refusals — a "
        "refusal that escaped as a new exception type would be the silent "
        "approval all over again"
    )
    assert "cannot fulfill your request" in str(caught.value), (
        "str(exc) is the model's own sentence — it travels into the run record"
    )


def test_a_truncated_array_is_unreadable_but_not_refused():
    with pytest.raises(AgentReplyUnreadable) as caught:
        _Agent()._parse_findings(TRUNCATED_ARRAY, None)
    assert not isinstance(caught.value, AgentRefused)


def test_an_empty_reply_is_unreadable_but_not_refused():
    with pytest.raises(AgentReplyUnreadable) as caught:
        _Agent()._parse_findings("", None)
    assert not isinstance(caught.value, AgentRefused)


def test_a_finding_that_says_cannot_is_a_finding():
    findings = _Agent()._parse_findings(FINDING_THAT_SAYS_CANNOT, None)
    assert len(findings) == 1
    assert findings[0].title == "None not handled"


# ─── The second call: re-framed, not corrected ───────────────────────


def test_the_re_framed_ask_carries_the_authorisation_and_not_the_reminder():
    ctx, client = _ctx([REAL_REFUSAL, VALID])
    _Agent().review(ctx)

    first, second = client.generate.call_args_list
    first_system = first.kwargs["system_instruction"]
    second_system = second.kwargs["system_instruction"]
    assert second_system.startswith(first_system), (
        "re-framed means appended to, not replaced"
    )
    # Asserted on the APPENDED TAIL, not on the whole second call and not on
    # the first call's absence of the phrase: the security agent's own base
    # prompt now opens with the same authorisation (so the first ask does not
    # provoke the refusal), and `_compose_effective_system_prompt` resolves
    # the registered prompt for agent "security" over this stub's. What this
    # test pins is the ladder's contribution — the re-frame is the second
    # ask's difference — not the wording of the agent's prompt.
    appended = second_system[len(first_system):]
    assert "authorised code review" in appended, (
        "the framing is the second ask's difference, not part of every ask"
    )
    assert "could not be parsed" not in appended


def test_the_fallback_after_a_refusal_gets_neither_the_reminder_nor_the_framing():
    ctx, client = _ctx([REAL_REFUSAL, VALID], fallback_model="backup")
    _Agent().review(ctx)

    first, second = client.generate.call_args_list
    assert second.kwargs["model"] == "backup"
    assert second.kwargs["system_instruction"] == first.kwargs["system_instruction"]
    # The addenda, not the phrase: the security agent's base prompt states
    # the authorisation itself, so "authorised code review" may well be in
    # BOTH calls — what must not be is either of the ladder's two addenda.
    assert LLMReviewAgent._REFUSAL_REFRAME.strip() not in second.kwargs["system_instruction"]
    assert "could not be parsed" not in second.kwargs["system_instruction"]


def test_neither_call_after_a_refusal_waits(clock):
    """The reply arrived; the content was the problem. A pause before the
    re-framed ask or the fallback would only hold the worker thread."""
    ctx, _ = _ctx([REAL_REFUSAL, VALID])
    _Agent().review(ctx)
    assert clock == []

    ctx, _ = _ctx([REAL_REFUSAL, VALID], fallback_model="backup")
    _Agent().review(ctx)
    assert clock == []


# ─── The run record: what the model said, redacted ───────────────────


def test_two_refusals_say_the_second_was_a_different_question():
    ctx, _ = _ctx([REAL_REFUSAL, REAL_REFUSAL])
    error = _Agent().review(ctx).error

    assert error is not None
    assert error.startswith("the model refused to review this change")
    assert "again when told the review is authorised" in error, (
        "one refusal is weather; two, the second of a re-framed ask, is a "
        "model that will not review this change — the operator's move is a "
        "fallback model, and the record should point there"
    )
    assert REAL_REFUSAL[:120] in error, "the model's own sentence, after the table's"
    assert error.endswith("…"), "clipped to MAX_HINT — the sentence, not the essay"


def test_a_refusal_that_echoes_a_secret_does_not_carry_it_into_the_record():
    """The refusal is model output, and the model had the diff. Whatever it
    quoted back goes through the redactor before it becomes an error string
    that reaches the run row."""
    leaked = "sk-live-" + "A" * 40
    refusal = (
        f"Sorry, I cannot help with code that contains {leaked} as a literal; "
        "you can search online for secret management guidance."
    )
    ctx, _ = _ctx([refusal, refusal])
    error = _Agent().review(ctx).error

    assert error is not None and "refused" in error
    assert leaked not in error
    assert "sk-live" not in error
    assert "[REDACTED:" in error


def test_a_refused_primary_and_a_refused_fallback_both_travel():
    ctx, _ = _ctx([REAL_REFUSAL, "I can't help with that."], fallback_model="backup")
    error = _Agent().review(ctx).error

    assert error is not None
    assert error.startswith("the model refused to review this change: Sorry, I cannot")
    assert "the fallback model refused too: I can't help with that." in error
