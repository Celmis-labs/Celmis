"""One corrective retry between "the reply was garbage" and "the agent failed".

The commonest unreadable reply is a findings array truncated by
max_output_tokens. That failure is deterministic — resending the same call
verbatim truncates at the same place — so the retry is corrective, not a
repeat: double the output budget, and append a reminder for the
nondeterministic failures (prose around the array, a reasoning trace).

Exactly one retry. A model that cannot produce the format twice in a row
with twice the room is not going to; the agent lands in agents_failed and
the verdict degrades honestly, which is the fix that landed before this one.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.review.agents.base import AgentContext, LLMReviewAgent
from src.review.models import Hunk, PullRequest

VALID = '[{"reasoning": "line 1 reads x before it is assigned", "file": "a.py", "line": 1, "severity": "critical", "title": "t", "description": "d"}]'
GARBAGE = "I found issues in [several] places but"  # truncated mid-thought


def _response(text: str) -> MagicMock:
    r = MagicMock()
    r.text = text
    r.input_tokens = 100
    r.output_tokens = 40
    r.cost_usd = 0.01
    r.cost_source = "litellm_estimate"
    r.model = "m"
    return r


def _ctx(replies: list[str]) -> tuple[AgentContext, MagicMock]:
    client = MagicMock()
    client.generate.side_effect = [_response(t) for t in replies]
    pr = PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="a", head_ref="f", head_sha="b",
        state="open",
        hunks=[Hunk(file_path="a.py", old_file_path="a.py", old_start=1,
                    old_count=1, new_start=1, new_count=1, content="@@")],
    )
    return AgentContext(pull_request=pr, llm_client=client), client


class _Agent(LLMReviewAgent):
    name = "security"
    system_prompt = "find problems"

    def _build_prompt(self, context):
        return "p"


def test_a_garbled_first_reply_is_given_one_corrected_second_chance():
    ctx, client = _ctx([GARBAGE, VALID])
    result = _Agent().review(ctx)

    assert result.error is None
    assert len(result.findings) == 1
    assert client.generate.call_count == 2

    first, second = client.generate.call_args_list
    assert second.kwargs["max_output_tokens"] == 2 * first.kwargs["max_output_tokens"], \
        "truncation is deterministic — a verbatim resend would truncate identically"
    assert "could not be parsed" in second.kwargs["system_instruction"]
    assert "could not be parsed" not in first.kwargs["system_instruction"]


def test_both_attempts_are_paid_for():
    """Tokens and cost accumulate across attempts — the ledger records what
    was spent, not what was useful."""
    ctx, _ = _ctx([GARBAGE, VALID])
    result = _Agent().review(ctx)
    assert result.tokens_in == 200
    assert result.tokens_out == 80
    assert result.cost_usd == 0.02


def test_a_second_garbled_reply_is_a_failure_not_a_loop():
    ctx, client = _ctx([GARBAGE, GARBAGE])
    result = _Agent().review(ctx)

    assert client.generate.call_count == 2, "exactly one retry, never a loop"
    assert result.error is not None and "corrective retry" in result.error
    assert result.findings == []
    assert result.tokens_in == 200, "the failed attempts still cost tokens"


def test_a_clean_first_reply_never_pays_for_a_second_call():
    ctx, client = _ctx([VALID, VALID])
    result = _Agent().review(ctx)
    assert client.generate.call_count == 1
    assert result.error is None and len(result.findings) == 1


def test_a_transport_error_on_the_retry_keeps_the_first_attempts_tokens():
    ctx, client = _ctx([GARBAGE])
    client.generate.side_effect = [_response(GARBAGE), RuntimeError("proxy 502")]
    result = _Agent().review(ctx)
    assert result.error == "proxy 502"
    assert result.tokens_in == 100, "attempt 1 was paid for and stays on the books"
