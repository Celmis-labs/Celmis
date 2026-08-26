"""Tokens an agent spent on its way to failing are still tokens we were billed for.

The aggregation loop read the error first and `continue`d, so the four lines
that add `tokens_in`, `tokens_out` and `cost_usd` to the batch were reachable
only by agents that succeeded. Everything a failing agent spent — prompt,
diff, graph summary, the model's half-finished reply — left no trace in the
run's cost. The run row showed the spend of the survivors and called it the
run's spend.

That was already wrong when an agent got exactly one call. It got worse the
moment `_generate_and_parse` started spending a SECOND call on an unreadable
reply: it accumulates across both attempts on purpose, precisely so that the
ledger records what was paid, and the orchestrator threw the sum away. The
more the retry helped, the more money went missing.

Byte counts here are arbitrary but distinct, so a total can only come out
right by adding the right things.
"""

from __future__ import annotations

import pytest

from src.review.agents.base import AgentContext, AgentRunResult, ReviewAgent
from src.review.agents.verifier import PrefilterResult, VerifierResult
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewRunStatus,
    ReviewVerdict,
)
from src.review.orchestrator import ReviewOrchestrator


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


class _Agent(ReviewAgent):
    """An agent that returns a canned AgentRunResult — the thing the loop reads."""

    def __init__(self, name: str, **result) -> None:
        self.name = name
        self._result = result

    def review(self, context: AgentContext) -> AgentRunResult:
        return AgentRunResult(agent=self.name, **self._result)


class _PassThroughVerifier:
    """Both halves of the stage the orchestrator calls — the deterministic
    prefilter it always runs and the LLM pass it runs by policy — as
    identity, so the loop under test is the only thing shaping the batch."""

    def prefilter(self, findings, **_):
        return PrefilterResult(kept=list(findings))

    def llm_pass(self, findings, context):
        return VerifierResult(kept=list(findings))


class _Provider:
    def fetch_pull_request(self, repo, number):
        return _pr()

    def post_review(self, batch, dry_run=False):  # pragma: no cover - not posted
        return {}

    def close(self):
        pass


@pytest.fixture
def run(monkeypatch):
    """Drive the real aggregation loop with no network and no database.

    The loop under test is buried in `_review_impl`, so the graph build, the
    policy load and the two post-agent stages are stubbed rather than the loop
    being copied into the test — a copy would keep passing after the original
    changed, which is the failure mode this whole file is about.
    """
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod

    monkeypatch.setattr(
        bc_mod, "run_breaking_change",
        lambda ctx: AgentRunResult(agent="breaking_change"),
    )
    monkeypatch.setattr(
        comp_mod, "run_compliance",
        lambda ctx: AgentRunResult(agent="compliance"),
    )

    def _run(*agents):
        orch = ReviewOrchestrator(agents=list(agents),
                                  verifier=_PassThroughVerifier())
        monkeypatch.setattr(orch, "_load_policy", lambda slug: None)
        monkeypatch.setattr(
            orch, "_build_context",
            lambda pr, **kw: AgentContext(pull_request=pr),
        )
        return orch.review(
            "github", "acme/api", 7,
            dry_run=True, post_comments=False, provider=_Provider(),
        ).batch

    return _run


# ─── the ledger ──────────────────────────────────────────────────────


def test_a_failed_agents_tokens_are_in_the_runs_total(run):
    batch = run(
        _Agent("architect", tokens_in=1000, tokens_out=200,
               cost_usd=0.05, cost_source="litellm_estimate"),
        _Agent("security", error="unreadable reply after a corrective retry",
               tokens_in=4000, tokens_out=900,
               cost_usd=0.42, cost_source="litellm_estimate"),
    )
    assert batch.tokens_in == 5000
    assert batch.tokens_out == 1100
    assert batch.cost_usd == pytest.approx(0.47)


def test_the_agent_is_still_counted_as_failed(run):
    """Paying for it does not mean it worked. The verdict guard and the run
    status both read these rosters, and folding the failure into `agents_run`
    to get the tokens would have re-opened the false approval."""
    batch = run(
        _Agent("architect", tokens_in=10, tokens_out=1, cost_usd=0.01),
        _Agent("security", error="quota exhausted", tokens_in=90, tokens_out=9,
               cost_usd=0.09),
    )
    assert batch.agents_run == ["architect"]
    assert batch.agents_failed == ["security"]
    assert batch.run_status is ReviewRunStatus.PARTIAL
    assert batch.verdict is not ReviewVerdict.APPROVE
    assert batch.tokens_in == 100


def test_a_failed_agents_findings_are_still_dropped(run):
    """The only thing that must NOT survive the error. Whatever a half-parsed
    reply left in `findings` was not reviewed material."""
    batch = run(
        _Agent("architect", tokens_in=10, tokens_out=1, cost_usd=0.01),
        _Agent(
            "security", error="unreadable reply", tokens_in=90, tokens_out=9,
            cost_usd=0.09,
            findings=[Finding(file_path="src/foo.py", line=2,
                              severity=FindingSeverity.CRITICAL,
                              title="from a reply nobody could read",
                              agent="security")],
        ),
    )
    assert batch.findings == []
    assert batch.tokens_in == 100


def test_the_bill_survives_every_agent_failing(run):
    """The retry doubles the calls on exactly this run. Losing the whole
    ledger for it is losing the most expensive run there is."""
    batch = run(
        _Agent("architect", error="rate limited", tokens_in=1200,
               tokens_out=300, cost_usd=0.11, cost_source="litellm_estimate"),
        _Agent("security", error="rate limited", tokens_in=1300,
               tokens_out=400, cost_usd=0.13, cost_source="litellm_estimate"),
    )
    assert batch.tokens_in == 2500
    assert batch.tokens_out == 700
    assert batch.cost_usd == pytest.approx(0.24)
    assert batch.run_status is ReviewRunStatus.FAILED
    assert batch.verdict is not ReviewVerdict.APPROVE


# ─── what "unknown cost" means ───────────────────────────────────────


def test_an_agent_that_never_sent_anything_does_not_blank_the_total(run):
    """`build_llm_client` raising (no key for the workspace, unknown model)
    returns a result with no tokens and no cost figure. That agent spent
    nothing, and nothing is a known quantity — treating its missing figure as
    "unknown" would erase the real, reportable spend of everyone else."""
    batch = run(
        _Agent("architect", tokens_in=800, tokens_out=100, cost_usd=0.07,
               cost_source="litellm_estimate"),
        _Agent("security", error="no LLM key configured for this workspace"),
    )
    assert batch.cost_usd == pytest.approx(0.07)
    assert batch.agents_failed == ["security"]


def test_an_agent_that_spent_tokens_with_no_price_makes_the_total_unknown(run):
    """The other direction, and the reason `cost_usd is None` meant anything
    in the first place: real tokens went out under a model we cannot price, so
    any number we print is a lie of omission."""
    batch = run(
        _Agent("architect", tokens_in=800, tokens_out=100, cost_usd=0.07,
               cost_source="litellm_estimate"),
        _Agent("security", error="unreadable reply", tokens_in=5000,
               tokens_out=1200, cost_usd=None),
    )
    assert batch.cost_usd is None
    assert batch.tokens_in == 5800, (
        "the tokens are known even when the price is not — that is the whole "
        "reason they are counted separately"
    )


def test_a_successful_agent_with_no_price_still_makes_the_total_unknown(run):
    """Unchanged behaviour, pinned because the branch that decides it was
    rewritten around the failure case."""
    batch = run(
        _Agent("architect", tokens_in=800, tokens_out=100, cost_usd=0.07),
        _Agent("security", tokens_in=500, tokens_out=60, cost_usd=None),
    )
    assert batch.cost_usd is None
    assert batch.agents_failed == []


def test_a_clean_run_totals_exactly_what_its_agents_spent(run):
    batch = run(
        _Agent("architect", tokens_in=800, tokens_out=100, cost_usd=0.07,
               cost_source="litellm_estimate"),
        _Agent("security", tokens_in=500, tokens_out=60, cost_usd=0.03,
               cost_source="litellm_estimate"),
    )
    assert (batch.tokens_in, batch.tokens_out) == (1300, 160)
    assert batch.cost_usd == pytest.approx(0.10)
    assert batch.cost_source == "litellm_estimate"
    assert batch.run_status is ReviewRunStatus.COMPLETE
