"""A review in which every stage failed is not "partial", and not an approval.

`run_status` asked one question — "did anything fail?" — and never the other
one, "did anything succeed?". So the two runs below came back wearing the same
word:

    agents_run=[architect, security, quality], agents_failed=[tests]
    agents_run=[],                             agents_failed=[architect,
                                                security, quality, tests]

The first is what PARTIAL was invented for: comments posted, one stage
missing. The second is an expired key, a provider outage, a quota wall — every
failure mode that hits all five agents in the same second — and the product
reported it as "we looked nearly everywhere". Worse, the verdict that rode
along with it was APPROVE whenever the agents that died were not on the
critical list: `compute_verdict` saw no findings and no failed critical agent,
`mark_complete` upgraded COMMENT to APPROVE, and a pull request nobody had
read got a green tick.

The banner had the same disease one layer down. Its non-critical arm was a
fixed sentence — "Every other stage completed; its comments are below." —
asserting a shape instead of describing the run. Both clauses were false for
the all-failed case, and the second was false for any clean partial review:
there are no comments below when nothing was found.

What is pinned here is the reading, not the prose: whatever the banner claims
about stages and comments has to be true of the batch that produced it.
"""

from __future__ import annotations

import re

import pytest

from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
    ReviewRunStatus,
    ReviewVerdict,
)

ALL_AGENTS = ["architect", "security", "quality", "tests", "structural"]


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


def _finished(**kw) -> ReviewBatch:
    """A batch finished the way the orchestrator finishes one."""
    b = ReviewBatch(pull_request=_pr(), **kw)
    b.verdict = b.compute_verdict()
    b.apply_partial_banner()
    b.mark_complete()
    return b


def _warning() -> Finding:
    return Finding(file_path="src/foo.py", line=2,
                   severity=FindingSeverity.WARNING,
                   title="naming", body="rename this", agent="quality")


# ─── nothing answered ────────────────────────────────────────────────


def test_a_run_where_every_agent_failed_is_failed_not_partial():
    b = _finished(agents_failed=list(ALL_AGENTS))
    assert b.nothing_ran
    assert b.run_status is ReviewRunStatus.FAILED


@pytest.mark.parametrize("failed", [
    pytest.param(ALL_AGENTS, id="the whole roster"),
    pytest.param(["quality", "tests"], id="only non-critical agents dispatched"),
    pytest.param(["quality"], id="a single non-critical agent, alone"),
    pytest.param(["claude_code"], id="the single-stage engine"),
])
def test_nothing_answered_so_the_verdict_is_not_an_approval(failed):
    """The nastiest arm is the non-critical one. `failed_critical_agents` is
    empty there, so every guard written for the Stage 11 false negative stands
    down: no findings, no failed critical agent, and `mark_complete` hands
    back APPROVE for a review that read nothing at all."""
    b = _finished(agents_failed=list(failed))
    assert b.run_status is ReviewRunStatus.FAILED
    assert b.verdict is not ReviewVerdict.APPROVE
    assert b.verdict is ReviewVerdict.SKIPPED, (
        "nothing was reviewed, and SKIPPED is the only verdict that says so "
        "without also making a claim about the code"
    )


def test_the_failed_run_is_not_upgraded_on_the_way_out():
    """`mark_complete` turns a COMMENT with nothing to comment on into an
    APPROVE. A run with nothing to comment on because nothing ran is exactly
    the input that rule must not be applied to."""
    b = ReviewBatch(pull_request=_pr(), agents_failed=list(ALL_AGENTS))
    b.verdict = b.compute_verdict()
    before = b.verdict
    b.mark_complete()
    assert b.verdict is before
    assert b.verdict is not ReviewVerdict.APPROVE


# ─── the cases that must NOT become failures ─────────────────────────


def test_one_stage_missing_is_still_partial():
    b = _finished(agents_run=["architect", "security", "quality"],
                  agents_failed=["tests"])
    assert not b.nothing_ran
    assert b.run_status is ReviewRunStatus.PARTIAL


def test_a_run_with_no_failures_is_complete():
    b = _finished(agents_run=list(ALL_AGENTS))
    assert b.run_status is ReviewRunStatus.COMPLETE
    assert b.verdict is ReviewVerdict.APPROVE


def test_a_review_skipped_before_any_agent_was_dispatched_is_not_a_failure():
    """Draft PR, no hunks, policy off: those paths set SKIPPED and return
    without touching either roster. Both lists empty means nothing was tried,
    which is not the same as everything having been tried and lost (FAILED).
    It used to read back COMPLETE, which is the opposite lie — "complete" is
    the word for a review that looked, and the same both-empty shape is what
    a policy disabling every agent leaves behind after falling through the
    whole pipeline. SKIPPED is the status that says what both are."""
    b = ReviewBatch(pull_request=_pr())
    b.verdict = ReviewVerdict.SKIPPED
    assert not b.nothing_ran
    assert b.nothing_dispatched
    assert b.run_status is ReviewRunStatus.SKIPPED


def test_a_deterministic_stage_that_answered_keeps_the_run_partial():
    """breaking_change and compliance reach `agents_run` without an LLM. If
    one of them produced findings, the run has real signal in it and is
    missing stages — the definition of partial."""
    b = _finished(agents_run=["breaking_change"],
                  agents_failed=["architect", "security", "quality"],
                  findings=[_warning()])
    assert b.run_status is ReviewRunStatus.PARTIAL
    assert b.verdict is not ReviewVerdict.SKIPPED


def test_a_finding_from_a_stage_that_answered_still_drives_the_verdict():
    critical = Finding(file_path="src/foo.py", line=2,
                       severity=FindingSeverity.CRITICAL,
                       title="SQL injection", agent="architect")
    b = _finished(agents_run=["architect"], agents_failed=["security"],
                  findings=[critical])
    assert b.verdict is ReviewVerdict.REQUEST_CHANGES


# ─── the banner says only what is true ───────────────────────────────


def _claims(banner: str) -> dict:
    """Read the banner the way the pull-request author would.

    Deliberately loose: this asks what a reader would come away believing, so
    that the assertions below are about the claim rather than about the
    sentence that carries it. Rewording is free; claiming something the batch
    cannot back is not.
    """
    return {
        # "…their 3 comments are below."
        "comments_below": "below" in banner,
        # "The 2 other stages that ran…" / "…no other stage produced an answer"
        "other_stages_ran": bool(re.search(r"\b[1-9]\d* other stages?\b", banner)),
        "nothing_was_reviewed": "NOT been reviewed" in banner,
        "counts": [int(n) for n in re.findall(r"\d+", banner)],
    }


@pytest.mark.parametrize("kw", [
    pytest.param(dict(agents_failed=["quality"]),
                 id="the failed agent was the only stage"),
    pytest.param(dict(agents_run=["architect", "security"],
                      agents_failed=["quality"]),
                 id="other stages ran and found nothing"),
    pytest.param(dict(agents_run=["architect"], agents_failed=["quality"]),
                 id="one other stage, nothing found"),
    pytest.param(dict(agents_run=["architect", "security"],
                      agents_failed=["quality"], findings=[_warning()]),
                 id="other stages ran and found something"),
    pytest.param(dict(agents_run=["quality"], agents_failed=["security"]),
                 id="a critical agent failed"),
    pytest.param(dict(agents_run=[], agents_failed=["architect", "security"]),
                 id="the critical agents were the only stages"),
])
def test_the_banner_never_claims_more_than_the_batch_can_back(kw):
    b = _finished(**kw)
    claims = _claims(b.partial_banner)

    if claims["comments_below"]:
        assert b.findings, "promised comments below a review that found none"
    if claims["other_stages_ran"]:
        assert b.agents_run, "credited stages that completed when none did"
    if claims["nothing_was_reviewed"]:
        assert not b.agents_run, "said nothing was reviewed while a stage answered"
    for n in claims["counts"]:
        assert n in {len(b.agents_run), len(b.findings)}, (
            f"the banner states {n}, which is neither the number of stages "
            f"that ran ({len(b.agents_run)}) nor the number of findings "
            f"({len(b.findings)})"
        )


def test_the_banner_agrees_with_the_status_it_ships_beside():
    """The run row carries `status` and `summary` in the same record, and the
    UI shows both. A banner headed PARTIAL over a row that says failed is the
    same class of contradiction this wave started with."""
    for kw in (dict(agents_failed=ALL_AGENTS),
               dict(agents_run=["architect"], agents_failed=["quality"]),
               dict(agents_run=["quality"], agents_failed=["security"])):
        b = _finished(**kw)
        partial_claimed = "PARTIAL REVIEW" in b.summary
        assert partial_claimed is (b.run_status is ReviewRunStatus.PARTIAL)


def test_a_clean_run_still_carries_no_banner():
    b = _finished(agents_run=list(ALL_AGENTS))
    assert b.partial_banner == ""
    assert "PARTIAL" not in b.summary
    assert "FAILED" not in b.summary


def test_the_critical_arm_is_untouched():
    """It was the accurate one: `compute_verdict` is right there refusing to
    approve, so "cannot be an approval" is a claim about code in the same
    file. Kept under test so a rewrite of the fallback cannot take it out."""
    b = _finished(agents_run=["quality", "tests"], agents_failed=["security"])
    assert "cannot be an approval" in b.partial_banner
    assert b.verdict is not ReviewVerdict.APPROVE
    for agent in b.agents_failed:
        assert agent in b.partial_banner


def test_the_banner_is_still_applied_at_most_once():
    b = ReviewBatch(pull_request=_pr(), agents_failed=list(ALL_AGENTS),
                    summary="Original body.")
    b.apply_partial_banner()
    b.apply_partial_banner()
    assert b.summary.count("did not run") == 1


# ─── it survives the run ─────────────────────────────────────────────


def test_the_failed_status_reaches_the_history_row(tmp_path):
    """FAILED has to be the word the row holds, because `status` is what the
    reviews page and any API consumer switch on. A batch that knows and a row
    that says "partial" is the gap this whole wave exists to close."""
    from src.api.review_runs import (
        ReviewRun,
        ReviewRunStore,
        record_completed_review,
    )

    class _Result:
        def __init__(self, batch):
            self.batch = batch
            self.posted = False

    store = ReviewRunStore(tmp_path / "review_runs.db")
    store.insert(ReviewRun(id="r1", user_id="u1", pr_ref="github:acme/api#7"))
    batch = _finished(agents_failed=list(ALL_AGENTS))
    record_completed_review(_Result(batch), run_id="r1", store=store)

    row = store.get("r1")
    assert row is not None
    assert row.status == ReviewRunStatus.FAILED.value
    assert row.verdict != ReviewVerdict.APPROVE.value
    assert row.agents_run == []
    assert row.agents_failed == ALL_AGENTS


def test_the_api_still_explains_a_run_that_failed_without_raising():
    """`_run_to_out` swaps `summary` for `error_message` on a failed row —
    safe while 'failed' was only ever written by the except-branch that fills
    `error_message` in. An all-agents-failed run finishes normally and has
    none, so the bare swap handed the caller a null where the explanation
    was."""
    from src.api.review_runs import ReviewRun
    from src.api.routers.reviews import _run_to_out

    batch = _finished(agents_failed=list(ALL_AGENTS))
    out = _run_to_out(ReviewRun(
        id="r2", user_id="u1", pr_ref="github:acme/api#8",
        status=batch.run_status.value, verdict=batch.verdict.value,
        summary=batch.summary[:500], agents_failed=list(ALL_AGENTS),
        agents_run=[],
    ))
    assert out.summary, "the run row's explanation was dropped on the way out"
    for agent in ALL_AGENTS:
        assert agent in out.summary


def test_a_genuine_crash_still_prefers_its_error_message():
    from src.api.review_runs import ReviewRun
    from src.api.routers.reviews import _run_to_out

    out = _run_to_out(ReviewRun(
        id="r3", user_id="u1", pr_ref="github:acme/api#9",
        status="failed", verdict="pending",
        summary="stale half-written summary",
        error_message="provider returned 502",
    ))
    assert out.summary == "provider returned 502"
