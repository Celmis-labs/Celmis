"""A review whose finder died must not say the code is fine.

`ReviewBatch._CRITICAL_AGENTS` decides two things: whether the verdict may be
APPROVE, and which arm of the partial banner the pull request is told. It read
`{"architect", "security"}` — and the Phase-18 restructure retired
`architect`, so the set went stale in the one direction that is silent.

Reproduced on the shipped bytes before the fix:

    agents_run=["cve","structural"]  agents_failed=["defect","contract"]
      → failed_critical_agents []
      → compute_verdict APPROVE
      → banner "The 2 other stages that ran found nothing to comment on."

`defect` produces 60% of every confirmed finding on the 50-PR benchmark. So
the agent that does most of the looking could fail on every pull request while
the product answered "approved" — the exact failure this set exists to
prevent, reintroduced by renaming its members. A live instance of the shape
turned up the same night: an overnight bench run had all three LLM agents fail
on all 13 PRs for want of a provider key.

THE FIRST TEST BELOW IS THE ONE THAT MATTERS. It derives the expected
membership from the orchestrator's own LLM roster instead of restating it, so
the next rename cannot go stale the same way. A test that listed
`{"defect", "contract", "security"}` by hand would have passed before this fix
too, for the wrong roster.
"""

from __future__ import annotations

import pytest

from src.review.models import PullRequest, ReviewBatch, ReviewVerdict


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="o/r", number=1, title="t", description="d",
        author="a", base_ref="main", base_sha="x", head_ref="f", head_sha="y",
        state="open",
    )


def _batch(*, failed: list[str], ran: list[str] | None = None,
           findings: list | None = None) -> ReviewBatch:
    b = ReviewBatch(pull_request=_pr())
    b.agents_run = list(ran if ran is not None else ["cve", "structural"])
    b.agents_failed = list(failed)
    b.findings = list(findings or [])
    return b


# ─── the invariant, derived not restated ─────────────────────────────


def test_every_llm_finder_is_critical():
    """The roster is the source. Any agent that calls a model to LOOK for
    defects is one whose silence we cannot read as health."""
    from src.review.agents.base import LLMReviewAgent
    from src.review.orchestrator import ReviewOrchestrator

    finders = {a.name for a in ReviewOrchestrator._default_agents()
               if isinstance(a, LLMReviewAgent)}
    assert finders, "no LLM finders found — did the roster move?"
    missing = finders - set(ReviewBatch._CRITICAL_AGENTS)
    assert not missing, (
        f"{sorted(missing)} can fail without blocking an APPROVE. An agent "
        f"that does the looking, failing silently, is the false negative this "
        f"set exists to prevent."
    )


def test_the_one_retired_critical_name_is_kept_and_the_others_are_not():
    """`architect` WAS critical, so a pre-restructure row keeps its meaning.

    `quality` and `tests` never were. The first draft of this fix added all
    three and `test_a_non_critical_gap_is_still_a_gap` caught it: promoting
    them retroactively would change what an old row MEANT, and a banner
    telling an author that a quality agent's absence blocked the approval is
    untrue. Their remit moved into `defect`, which is critical on its own
    account rather than by inheritance.
    """
    assert "architect" in ReviewBatch._CRITICAL_AGENTS
    for never_critical in ("quality", "tests"):
        assert never_critical not in ReviewBatch._CRITICAL_AGENTS


# ─── the verdict ─────────────────────────────────────────────────────


@pytest.mark.parametrize("failed", [
    ["defect"], ["contract"], ["security"],
    ["defect", "contract"], ["defect", "contract", "security"],
])
def test_a_failed_finder_blocks_the_approval(failed):
    b = _batch(failed=failed)
    b.verdict = b.compute_verdict()
    b.mark_complete()
    assert b.verdict != ReviewVerdict.APPROVE, (
        f"{failed} failed and the review still approved the change"
    )


def test_mark_complete_does_not_upgrade_it_back():
    """The second half of the same hole: `compute_verdict` chose COMMENT
    *because* a finder is missing, and `mark_complete` used to promote a
    finding-free COMMENT to APPROVE."""
    b = _batch(failed=["defect"])
    b.verdict = b.compute_verdict()
    assert b.verdict == ReviewVerdict.COMMENT
    b.mark_complete()
    assert b.verdict == ReviewVerdict.COMMENT


def test_a_clean_run_still_approves():
    """The guard must not become a blanket refusal — a review where every
    stage answered and found nothing is an approval."""
    b = _batch(failed=[], ran=["defect", "contract", "security", "structural"])
    b.verdict = b.compute_verdict()
    b.mark_complete()
    assert b.verdict == ReviewVerdict.APPROVE


def test_a_deterministic_stage_failing_does_not_block():
    """`cve` is deliberately not critical: an install without the osv binary
    and offline would otherwise downgrade EVERY review."""
    b = _batch(failed=["cve"], ran=["defect", "contract", "security"])
    b.verdict = b.compute_verdict()
    b.mark_complete()
    assert b.verdict == ReviewVerdict.APPROVE


# ─── and what the pull request is told ───────────────────────────────


def test_the_banner_names_the_dead_finder_as_critical():
    b = _batch(failed=["defect"])
    banner = b.partial_banner
    assert "defect" in banner
    assert "cannot be an approval" in banner, (
        f"the banner took the non-critical arm: {banner!r}"
    )


def test_the_banner_does_not_claim_a_review_happened():
    """Two failed finders, two deterministic stages, nothing found. The old
    wording read 'The 2 other stages that ran found nothing to comment on.' —
    true, and read by an author as 'nothing is wrong'."""
    b = _batch(failed=["defect", "contract"])
    banner = b.partial_banner
    assert "cannot be an approval" in banner
    assert "found nothing to comment on" not in banner
