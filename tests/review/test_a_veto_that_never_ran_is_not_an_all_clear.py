"""A verifier that timed out and one that approved everything looked identical.

The LLM veto fails OPEN — an unreachable or unreadable verifier keeps every
finding — and that is the right call: a stage that cannot answer must not be
allowed to delete a real defect. It was, until now, an invisible call. Both
fail-open paths returned the untouched list and wrote a `logger.warning`, so
the batch, the verdict, the run row and the posted comment were byte-identical
to a review whose veto ran and found nothing worth dropping.

That matters most exactly when it happens. The veto exists to thin false
positives, so a review where it silently did not run is the review carrying the
most unfiltered noise — and the author reading that noise had no way to learn
it was never filtered. On this install the verifier asks for a large structured
reply from a reasoning model over every finding in the review: it is the
slowest single call the pipeline makes, and it was making it under the same
unpassed 120-second default as everything else.

`agents_failed`, not `agents_skipped`. "verifier" already appears in the
skipped list when a policy switches the veto off — a decision. This is the
other thing: asked to run, fell over. Same batch either way, different owner.

Not critical, and deliberately: a veto that did not run leaves MORE findings
standing, never fewer, so it must not block an approval the findings support.
"""

from __future__ import annotations

import pytest

from src.review.agents.base import AgentContext
from src.review.agents.verifier import VerifierAgent, VerifierResult
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewVerdict,
)
from src.review.orchestrator import ReviewOrchestrator
from src.review.settings import ReviewSettings


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open", raw_diff="@@ -1 +1,2 @@\n line\n+added\n",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


def _findings(n: int = 4) -> list[Finding]:
    return [Finding(
        file_path="src/foo.py", line=i + 1, severity=FindingSeverity.WARNING,
        title=f"claim {i}", body="b", agent="defect",
    ) for i in range(n)]


class _Client:
    """An LLM client whose one call does whatever the test says."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.timeouts: list[float | None] = []

    def generate(self, **kw):
        self.timeouts.append(kw.get("timeout"))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return _Reply(self._outcome)


class _Reply:
    def __init__(self, text): self.text, self.input_tokens, self.output_tokens = text, 10, 5


def _pass(outcome, findings=None):
    ctx = AgentContext(pull_request=_pr(), llm_client=_Client(outcome))
    return VerifierAgent().llm_pass(findings or _findings(), ctx), ctx.llm_client


# ─── the failure travels ─────────────────────────────────────────────


def test_a_result_can_say_the_veto_did_not_run():
    assert "error" in VerifierResult.__dataclass_fields__
    assert VerifierResult(kept=[]).error is None, (
        "None is 'the veto ran'; a sentence is 'it could not'"
    )


def test_an_unreachable_verifier_keeps_everything_and_says_why():
    result, _ = _pass(TimeoutError("too slow"))

    assert len(result.kept) == 4, "fail open is the correct behaviour"
    assert result.error, "and it must not be a silent one"


def test_an_unreadable_reply_is_the_same_kind_of_open():
    """The other fail-open, and it was just as invisible: the call succeeded,
    was paid for, and its reply could not be read."""
    result, _ = _pass("this is not a list of indices")

    assert len(result.kept) == 4
    assert result.error


def test_a_veto_that_ran_reports_no_error():
    """The distinction has to cut both ways, or it is decoration."""
    result, _ = _pass("[0, 1]")

    assert result.error is None
    assert len(result.kept) == 2


def test_the_sentence_is_ours_not_the_providers():
    """A provider's exception text is not something this product puts in front
    of a user — the whole reason the errors module exists."""
    result, _ = _pass(RuntimeError("secret-bearing upstream trace"))

    assert "secret-bearing upstream trace" not in (result.error or "")


# ─── and reaches the run ─────────────────────────────────────────────


class _Verifier:
    def __init__(self, result): self._result = result

    def prefilter(self, findings, **_):
        from src.review.agents.verifier import PrefilterResult
        return PrefilterResult(kept=list(findings))

    def llm_pass(self, findings, context):
        self._result.kept = list(findings)
        return self._result


class _Provider:
    def fetch_pull_request(self, repo, number): return _pr()

    def post_review(self, batch, dry_run=False): return {}

    def close(self): pass


@pytest.fixture
def run(monkeypatch):
    import src.review.breaking_change as bc_mod
    import src.review.compliance as comp_mod
    from src.review.agents.base import AgentRunResult, ReviewAgent

    monkeypatch.setattr(bc_mod, "run_breaking_change",
                        lambda ctx: AgentRunResult(agent="breaking_change"))
    monkeypatch.setattr(comp_mod, "run_compliance",
                        lambda ctx: AgentRunResult(agent="compliance"))

    class _Agent(ReviewAgent):
        name = "defect"

        def review(self, context):
            return AgentRunResult(agent="defect", findings=_findings())

    def _run(v_result):
        orch = ReviewOrchestrator(ReviewSettings(), agents=[_Agent()],
                                  verifier=_Verifier(v_result))
        # The veto is off unless a repository asks for it. A veto that was
        # never asked to run cannot have fallen over, so every test below
        # starts from a repository that asked.
        monkeypatch.setattr(orch, "_load_policy", lambda slug: {
            "enabled": True, "target_branches": [], "verifier_enabled": True,
        })
        monkeypatch.setattr(orch, "_build_context",
                            lambda pr, **kw: AgentContext(pull_request=pr))
        return orch.review("github", "acme/api", 7, dry_run=True,
                           post_comments=False, provider=_Provider()).batch

    return _run


def test_a_failed_veto_is_named_on_the_run(run):
    batch = run(VerifierResult(kept=[], error="the verifier's reply could not be read"))

    assert "verifier" in batch.agents_failed


def test_a_failed_veto_is_failed_not_skipped(run):
    """`agents_skipped` means a policy switched the veto off. Collapsing the
    two would report an operator's decision as an outage, and an outage as a
    decision."""
    batch = run(VerifierResult(kept=[], error="timed out"))

    assert "verifier" not in batch.agents_skipped


def test_the_reader_of_the_pull_request_is_told(run):
    """A repository that ASKED for the veto and did not get it is a gap, and
    the gap notice is where gaps are said. (A repository that never asked is
    not told anything — the veto is off by default, and an unconditional line
    about a stage nobody enabled is noise on every pull request.)"""
    batch = run(VerifierResult(kept=[], error="timed out"))

    assert "verifier" in batch.summary


def test_a_veto_that_ran_leaves_the_run_clean(run):
    batch = run(VerifierResult(kept=[], error=None))

    assert "verifier" not in batch.agents_failed


def test_a_failed_veto_does_not_block_an_approval(run):
    """It fails open, so it leaves MORE findings standing, never fewer. If the
    findings support an approval, a veto that did not run is no reason to
    withhold one — unlike a finder, whose absence means nobody looked."""
    from src.review.models import ReviewBatch

    assert "verifier" not in ReviewBatch._CRITICAL_AGENTS
    batch = run(VerifierResult(kept=[], error="timed out"))
    assert not batch.failed_critical_agents
    assert batch.verdict is not ReviewVerdict.SKIPPED, (
        "a review whose finder answered is a review that looked"
    )


# ─── and it is no longer making that call at 120 seconds ─────────────


def test_the_veto_call_carries_a_deadline():
    """It is the slowest single call the pipeline makes: a large structured
    reply from a reasoning model over every finding in the review."""
    _, client = _pass("[0, 1]")

    assert client.timeouts and client.timeouts[0], (
        "the veto inherited generate()'s 120s default, which no operator "
        "could reach"
    )
    assert client.timeouts[0] == ReviewSettings().llm_timeout_seconds
