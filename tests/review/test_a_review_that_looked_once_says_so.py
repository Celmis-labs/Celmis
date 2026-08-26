"""A one-pass review published as a two-pass one.

`DefectAgent` reads the diff twice; the second pass was measured at roughly 8pp
of recall (run G4). It is allowed to fail without taking the review with it,
and that is right — pass one's findings are real whatever happens next. What it
was not allowed to do is fail in silence, and it did:

    if later.error:
        logger.warning("defect_second_pass_failed …")
        return first

One `return` dropped four separate facts.

  1. The review published as COMPLETE, with nothing in `agents_failed`, no
     banner and an APPROVE available to it — byte-identical to a review that
     looked twice. The recall it lost was invisible to everyone.
  2. The second call's tokens and cost went missing, and it costs the MOST
     exactly when it fails: the call, the transient retry, and sometimes a
     fallback call on top, every one billed. `_generate_and_parse` sums across
     all of them on purpose so a failing agent does not read as a cheap one;
     dropping the sum here turned that honesty into a bigger hole.
  3. `parameter_adjustments` from the second call vanished — on BOTH branches,
     so a ceiling clamp or a swap to the fallback model disappeared even when
     the pass worked.
  4. (Not this file.) The pass runs before the review's wall-clock gate, so an
     OPTIONAL second look can spend the budget the mandatory stages needed.
     That is a priority question, not a bug, and is left to a decision.

NOT `agents_failed`. The agent did not fail. Putting "defect" there would make
a critical finder read as absent and refuse an approval its own findings
support. What is missing is a stage INSIDE the agent, so it is named as one.
"""

from __future__ import annotations

import pytest

from src.llm.capabilities import ADJUST_CLAMPED, PARAM_MAX_OUTPUT_TOKENS, ParameterAdjustment
from src.review.agents.base import AgentContext, AgentRunResult
from src.review.agents.defect import DefectAgent
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
)

STAGE = "defect_second_pass"


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


def _f(title: str) -> Finding:
    return Finding(file_path="src/foo.py", line=1,
                   severity=FindingSeverity.WARNING, title=title,
                   body="b", agent="defect")


def _clamp() -> ParameterAdjustment:
    return ParameterAdjustment(
        agent="defect", parameter=PARAM_MAX_OUTPUT_TOKENS,
        requested=8192, sent=4096, action=ADJUST_CLAMPED,
        reason="the model's ceiling is lower",
    )


@pytest.fixture
def two_passes(monkeypatch):
    """`DefectAgent.review` with the two underlying calls scripted."""

    def _run(first: AgentRunResult, later: AgentRunResult) -> AgentRunResult:
        calls = iter((first, later))
        monkeypatch.setattr(
            "src.review.agents.base.LLMReviewAgent.review",
            lambda self, context: next(calls),
        )
        agent = DefectAgent(passes=2)
        return agent.review(AgentContext(pull_request=_pr()))

    return _run


# ─── the ledger survives a failing pass ──────────────────────────────


def test_a_failed_second_pass_still_costs_what_it_cost(two_passes):
    out = two_passes(
        AgentRunResult(agent="defect", findings=[_f("real")],
                       tokens_in=100, tokens_out=50, cost_usd=0.01),
        AgentRunResult(agent="defect", error="timed out", error_code="local_timeout",
                       tokens_in=90, tokens_out=40, cost_usd=0.009),
    )
    assert out.tokens_in == 190
    assert out.tokens_out == 90
    assert out.cost_usd == pytest.approx(0.019)


def test_tokens_with_no_price_make_the_total_unknown_not_smaller(two_passes):
    """Nothing is a known quantity; an unpriced call is not nothing."""
    out = two_passes(
        AgentRunResult(agent="defect", findings=[_f("real")], cost_usd=0.01,
                       cost_source="litellm_estimate"),
        AgentRunResult(agent="defect", error="boom", error_code="generation_failed",
                       tokens_in=90, tokens_out=40, cost_usd=None),
    )
    assert out.cost_source == "unknown"


def test_an_adjustment_on_the_failing_pass_is_not_lost(two_passes):
    out = two_passes(
        AgentRunResult(agent="defect", findings=[_f("real")]),
        AgentRunResult(agent="defect", error="timed out", error_code="local_timeout",
                       parameter_adjustments=[_clamp()]),
    )
    assert len(out.parameter_adjustments) == 1


def test_an_adjustment_on_a_pass_that_WORKED_is_not_lost_either(two_passes):
    """The half nobody would have looked for: the merge branch dropped these
    too, so a clamp on the second call vanished even on the happy path."""
    out = two_passes(
        AgentRunResult(agent="defect", findings=[_f("first")]),
        AgentRunResult(agent="defect", findings=[_f("second")],
                       parameter_adjustments=[_clamp()]),
    )
    assert len(out.parameter_adjustments) == 1
    assert len(out.findings) == 2


# ─── and the review says it looked once ──────────────────────────────


def test_the_missing_pass_is_named(two_passes):
    out = two_passes(
        AgentRunResult(agent="defect", findings=[_f("real")]),
        AgentRunResult(agent="defect", error="timed out", error_code="local_timeout"),
    )
    assert STAGE in out.skipped_stages


def test_it_carries_a_reason_fit_to_publish(two_passes):
    """`curated_reason`, not the provider's own message: this ends up in a
    public pull-request comment."""
    out = two_passes(
        AgentRunResult(agent="defect", findings=[_f("real")]),
        AgentRunResult(agent="defect", error="Timeout: upstream said <secret>",
                       error_code="local_timeout"),
    )
    assert "<secret>" not in out.skipped_stages[STAGE]
    assert "timeout" in out.skipped_stages[STAGE].lower()


def test_an_unnameable_failure_still_gets_a_sentence(two_passes):
    """A code with no curated row must not leave the stage unexplained — and
    must not borrow the provider's words either."""
    out = two_passes(
        AgentRunResult(agent="defect", findings=[_f("real")]),
        AgentRunResult(agent="defect", error="???", error_code="something_new"),
    )
    assert out.skipped_stages[STAGE]


def test_the_agent_itself_is_not_marked_failed(two_passes):
    """"defect" in `agents_failed` would make a critical finder read as absent
    and refuse an approval its own findings support."""
    out = two_passes(
        AgentRunResult(agent="defect", findings=[_f("real")]),
        AgentRunResult(agent="defect", error="timed out", error_code="local_timeout"),
    )
    assert out.error is None
    assert [f.title for f in out.findings] == ["real"]


def test_a_healthy_two_pass_review_names_nothing(two_passes):
    out = two_passes(
        AgentRunResult(agent="defect", findings=[_f("first")]),
        AgentRunResult(agent="defect", findings=[_f("second")]),
    )
    assert not out.skipped_stages


# ─── the reader of the pull request is told ──────────────────────────


def _batch(skipped=(), errors=None, failed=()) -> ReviewBatch:
    b = ReviewBatch(pull_request=_pr())
    b.agents_run = ["defect"]
    b.agents_skipped = list(skipped)
    b.agent_errors = dict(errors or {})
    b.agents_failed = list(failed)
    b.findings = [_f("real")]
    return b


def test_a_thinner_review_says_it_is_thinner():
    banner = _batch(skipped=[STAGE], errors={STAGE: "the model timed out"}).partial_banner

    assert "thinner" in banner
    assert STAGE in banner
    assert "the model timed out" in banner


def test_a_configured_skip_says_nothing():
    """`agents_skipped` also holds the veto this installation does not run by
    default and the tail stages a budget stood down. Only a skip with a REASON
    is news — configuration has none, because nothing went wrong. No name
    matching, so renaming a stage cannot quietly retire this."""
    banner = _batch(skipped=["verifier", "breaking_change", "compliance"]).partial_banner

    assert "thinner" not in banner


def test_a_failed_agent_is_not_reported_twice():
    """A stage that is in both lists is a failure, and the failure notice
    already names it."""
    banner = _batch(skipped=["security"], errors={"security": "provider quota exhausted"},
                    failed=["security"]).partial_banner

    assert "thinner" not in banner
    assert "provider quota exhausted" in banner


def test_it_reaches_the_posted_comment():
    from src.review.providers.base import _format_summary

    posted = _format_summary(
        _batch(skipped=[STAGE], errors={STAGE: "the model timed out"}),
        "<!-- celmis -->",
    )
    assert "thinner" in posted
