"""One diff, two reads, findings unioned — and why it is two and not a longer
prompt.

MEASURED, in the order the hypotheses were tested and discarded:

  * Folding three finder agents into one halved the volume — 4.08 findings
    per PR to 2.08 across 50 PRs — while precision per claim ROSE (defect
    alone 57.0%, better than architect 51%, quality 38%, tests 0%). So the
    loss was not judgement.
  * FIRST HYPOTHESIS, FALSIFIED: one answer has a natural list length, so the
    agent was seeing more than it wrote. Tested by stripping every restraint
    from the prompt and adding the per-line sweep already proven on the
    security agent. Result on 14 PRs: +0.35 findings per PR, ZERO new true
    positives, three new false ones. The agent was writing what it saw.
  * WHAT IS LEFT: sampling. The same benchmark had measured 43 of 43 findings
    lost between two runs of identical code as "this time it did not write
    it" — per-finding variance, not per-finding quality. Three agents were
    three draws on an overlapping pool; one agent is one draw. On the run
    data, architect+quality+tests took 66 true positives and defect alone
    takes 45, so one draw recovers p ≈ 0.68 and 1-(1-p)² ≈ 0.90.

That is why the second pass asks for the COMPLEMENT rather than for more: an
uninformed second draw re-finds what the first already has, the prefilter
collapses the duplicates, and the call buys nothing.
"""

from __future__ import annotations

import pytest

from src.review.agents.base import AgentContext, AgentRunResult
from src.review.agents.defect import DefectAgent
from src.review.models import Finding, FindingSeverity, Hunk, PullRequest


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=7, title="t", description="d",
        author="a", base_ref="main", base_sha="x", head_ref="f", head_sha="y",
        state="open",
        hunks=[Hunk(file_path="src/a.py", old_file_path="src/a.py",
                    old_start=1, old_count=1, new_start=1, new_count=2,
                    content="@@ -1 +1,2 @@\n line\n+added\n")],
    )


def _finding(title: str, line: int = 1) -> Finding:
    return Finding(
        agent="defect", file_path="src/a.py", line=line,
        severity=FindingSeverity.ERROR, title=title, body="b",
        rule_id="defect.x", confidence=0.9, reasoning=f"line {line}: {title}",
    )


class _Recorder:
    """Stands in for `LLMReviewAgent.review`, one scripted reply per call."""

    def __init__(self, results: list[AgentRunResult]):
        self.results = list(results)
        self.prompts: list[str] = []

    def __call__(self, agent, context):
        self.prompts.append(agent._build_prompt(context))
        return self.results.pop(0)


@pytest.fixture
def two_passes(monkeypatch):
    def _install(results):
        rec = _Recorder(results)
        monkeypatch.setattr(
            "src.review.agents.base.LLMReviewAgent.review",
            lambda self, context: rec(self, context),
        )
        return rec
    return _install


# ─── the union ───────────────────────────────────────────────────────


def test_both_passes_findings_are_kept(two_passes):
    rec = two_passes([
        AgentRunResult(agent="defect", findings=[_finding("first", 1)],
                       tokens_in=100, tokens_out=20, cost_usd=0.01),
        AgentRunResult(agent="defect", findings=[_finding("second", 2)],
                       tokens_in=140, tokens_out=25, cost_usd=0.02),
    ])
    out = DefectAgent().review(AgentContext(pull_request=_pr()))

    assert [f.title for f in out.findings] == ["first", "second"]
    assert len(rec.prompts) == 2


def test_the_cost_of_both_calls_is_reported(two_passes):
    """A second call the ledger cannot see is a review that looks cheaper
    than it is."""
    two_passes([
        AgentRunResult(agent="defect", findings=[], tokens_in=100,
                       tokens_out=20, cost_usd=0.01),
        AgentRunResult(agent="defect", findings=[], tokens_in=140,
                       tokens_out=25, cost_usd=0.02),
    ])
    out = DefectAgent().review(AgentContext(pull_request=_pr()))

    assert out.tokens_in == 240
    assert out.tokens_out == 45
    assert out.cost_usd == pytest.approx(0.03)


# ─── what the second pass is told ────────────────────────────────────


def test_the_second_prompt_lists_what_the_first_found(two_passes):
    rec = two_passes([
        AgentRunResult(agent="defect", findings=[_finding("off-by-one", 42)]),
        AgentRunResult(agent="defect", findings=[]),
    ])
    DefectAgent().review(AgentContext(pull_request=_pr()))

    first, second = rec.prompts
    assert "Already reported" not in first
    assert "src/a.py:42 — off-by-one" in second


def test_the_second_prompt_asks_for_the_complement(two_passes):
    """Not "find more". An uninformed second draw re-finds the first draw's
    findings and the prefilter throws them away."""
    rec = two_passes([
        AgentRunResult(agent="defect", findings=[_finding("x")]),
        AgentRunResult(agent="defect", findings=[]),
    ])
    DefectAgent().review(AgentContext(pull_request=_pr()))

    second = rec.prompts[1]
    assert "the defects the first pass did not" in second
    assert "Do not repeat them" in second


def test_the_second_pass_may_answer_nothing(two_passes):
    """Load-bearing. A prompt that lists findings and asks for more reads as a
    demand for one more, and the cheapest one more is a restatement — which is
    exactly what the per-line sweep produced when it had no bar."""
    rec = two_passes([
        AgentRunResult(agent="defect", findings=[_finding("x")]),
        AgentRunResult(agent="defect", findings=[]),
    ])
    out = DefectAgent().review(AgentContext(pull_request=_pr()))

    # Line wraps are layout, not meaning — compare the words.
    flat = " ".join(rec.prompts[1].split())
    assert "`[]` is the correct answer when the first pass was thorough" in flat
    assert "it is not a failure to agree with it" in flat
    assert len(out.findings) == 1


# ─── failure behaviour ───────────────────────────────────────────────


def test_a_failed_second_pass_keeps_the_first_passs_findings(two_passes):
    """An outage in the extra look must not cost the review what it already
    had."""
    two_passes([
        AgentRunResult(agent="defect", findings=[_finding("real")]),
        AgentRunResult(agent="defect", findings=[], error="provider 503"),
    ])
    out = DefectAgent().review(AgentContext(pull_request=_pr()))

    assert [f.title for f in out.findings] == ["real"]
    assert out.error is None


def test_a_failed_first_pass_does_not_buy_a_second_call(two_passes):
    rec = two_passes([
        AgentRunResult(agent="defect", findings=[], error="provider 429"),
    ])
    out = DefectAgent().review(AgentContext(pull_request=_pr()))

    assert out.error == "provider 429"
    assert len(rec.prompts) == 1


# ─── the switch ──────────────────────────────────────────────────────


def test_one_pass_is_configurable(two_passes):
    rec = two_passes([AgentRunResult(agent="defect", findings=[_finding("x")])])
    out = DefectAgent(passes=1).review(AgentContext(pull_request=_pr()))

    assert len(rec.prompts) == 1
    assert len(out.findings) == 1


def test_the_default_comes_from_settings():
    from src.review.settings import ReviewSettings

    assert ReviewSettings().defect_passes == 2
    assert DefectAgent().passes == 2


def test_a_nonsense_pass_count_cannot_disable_the_agent():
    """0 or -1 in the environment must still read the diff once."""
    assert DefectAgent(passes=0).passes == 1
    assert DefectAgent(passes=-3).passes == 1


def test_only_the_defect_agent_does_this():
    """Contract and security are single-draw by design: contract's remit is
    rare enough that a second look mostly re-reads an empty pool, and security
    was not the agent that lost recall."""
    from src.review.agents.contract import ContractAgent
    from src.review.agents.security import SecurityAgent

    for cls in (ContractAgent, SecurityAgent):
        assert not hasattr(cls, "passes"), cls.__name__
