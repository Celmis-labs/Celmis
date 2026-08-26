"""A review in which NO agent ran must not come back as an approval.

The shipped route: a per-repo policy whose `disabled_agents` names every
agent. `_run_agents_parallel` dispatches nothing and returns [] — no results,
no errors — so the aggregation loop never runs, both rosters stay empty, and
control falls through to `compute_verdict`, which saw no findings and
APPROVEd. The run row said "complete". The banner was "". The posted comment
read "✅ APPROVED — no blocking findings" over "_No issues detected._" — a
green tick on a pull request nothing had looked at.

The second half is the posted comment itself. `_format_summary` composed it
from findings, scope and telemetry and read neither `summary` nor the banner,
so even the reviews that KNEW they were partial/failed/skipped told the
author nothing: the gap notice reached the run row, the notification body and
the MCP payload — every surface except the one the pull-request author
actually reads.

Pinned here:

  * zero agents dispatched → verdict SKIPPED, status SKIPPED (not COMPLETE:
    nothing looked; not FAILED: nothing errored), and a banner naming the one
    cause an operator can act on;
  * the posted comment carries the very banner the row derives — one source
    of truth (`partial_banner`), no second wording to drift;
  * "_No issues detected._" is a claim that something looked, and is
    unreachable when nothing ran.
"""

from __future__ import annotations

from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
    ReviewRunStatus,
    ReviewVerdict,
)
from src.review.providers.base import _format_summary

ALL_AGENTS = ["architect", "security", "quality", "tests", "structural"]

_MARKER = "<!-- code-analyzer:review -->"


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


# ─── the dispatch gate ───────────────────────────────────────────────


class _NeverDispatched:
    """An agent whose dispatch is the test failure."""

    def __init__(self, name: str) -> None:
        self.name = name

    def review(self, context):  # pragma: no cover — dispatching it IS the bug
        raise AssertionError(f"{self.name} was dispatched despite the policy")


def test_disabling_the_whole_roster_dispatches_nothing_and_flags_nothing():
    """The shape the policy leaves behind: no results, and — because a
    switched-off agent is not a crashed one — no entries in `agents_failed`
    either. Everything downstream starts from this []."""
    from src.review.agents import VerifierAgent
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.settings import ReviewSettings

    orch = ReviewOrchestrator(
        settings=ReviewSettings(),
        agents=[_NeverDispatched(n) for n in ALL_AGENTS],
        verifier=VerifierAgent(),
    )
    results = orch._run_agents_parallel(None, disabled_agents=set(ALL_AGENTS))
    assert results == []


def test_an_empty_roster_handed_in_is_an_empty_roster():
    """The other way to reach zero agents, and the one that did not work.

    `self.agents = agents or self._default_agents()` read an EXPLICIT empty
    list as "the caller said nothing" and dispatched the full default five —
    the absent-versus-empty confusion, here inverting the caller's decision
    instead of merely losing it. It also quietly falsified every test that
    passed `agents=[]` to mean "no agents": each was running the real roster
    against a workspace with no API key, so what they measured was the
    no-key failure path, not the empty-roster one.

    `None` still means "give me the defaults" — that is what the default
    argument is for."""
    from src.review.agents import VerifierAgent
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.settings import ReviewSettings

    def _orch(**kw):
        return ReviewOrchestrator(settings=ReviewSettings(),
                                  verifier=VerifierAgent(), **kw)

    assert _orch(agents=[]).agents == []
    assert _orch(agents=None).agents, "None is 'unset', and unset means defaults"
    assert _orch().agents, "and so is leaving it out"


# ─── what the batch then claims ──────────────────────────────────────


def test_a_review_that_dispatched_zero_agents_is_skipped_not_approved():
    b = _finished()  # both rosters empty — the fall-through shape
    assert b.nothing_dispatched
    assert b.verdict is ReviewVerdict.SKIPPED, (
        "APPROVE here is a green tick on a pull request nothing looked at"
    )
    assert b.run_status is ReviewRunStatus.SKIPPED, (
        "COMPLETE is the word for a review that looked; FAILED is for one "
        "that errored — this one did neither"
    )


def test_the_banner_names_the_cause_the_operator_can_fix():
    b = _finished()
    assert "every agent is disabled for this repository" in b.partial_banner
    assert "NOT been reviewed" in b.partial_banner
    assert "PARTIAL" not in b.partial_banner, (
        "there is no review for this to be a part of"
    )
    assert b.summary.startswith(b.partial_banner), (
        "the notice has to survive into the summary the row and the "
        "notification carry"
    )


def test_mark_complete_does_not_upgrade_the_skip():
    b = ReviewBatch(pull_request=_pr())
    b.verdict = b.compute_verdict()
    before = b.verdict
    b.mark_complete()
    assert b.verdict is before
    assert b.verdict is not ReviewVerdict.APPROVE


def test_a_deterministic_stage_that_answered_makes_it_a_real_review():
    """breaking_change and compliance run regardless of `disabled_agents`.
    If one of them produced findings, something DID look — the verdict comes
    from the findings and the skip machinery must stand down."""
    b = _finished(agents_run=["breaking_change"], findings=[_warning()])
    assert not b.nothing_dispatched
    assert b.verdict is ReviewVerdict.COMMENT
    assert b.run_status is ReviewRunStatus.COMPLETE
    assert b.partial_banner == ""


def test_the_skipped_status_reaches_the_history_row(tmp_path):
    """SKIPPED has to be the word the row holds: `status` is what the reviews
    page and the usage rollup switch on, and 'complete' rows are counted as
    completed reviews. A repository nobody reviews must not produce the same
    history as one that is reviewed."""
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
    record_completed_review(_Result(_finished()), run_id="r1", store=store)

    row = store.get("r1")
    assert row is not None
    assert row.status == ReviewRunStatus.SKIPPED.value
    assert row.verdict == ReviewVerdict.SKIPPED.value
    assert "every agent is disabled for this repository" in row.summary


# ─── the posted comment says so too ──────────────────────────────────


def test_the_posted_comment_says_nothing_was_reviewed():
    comment = _format_summary(_finished(), _MARKER)
    assert "every agent is disabled for this repository" in comment
    assert "NOT been reviewed" in comment
    assert "_No issues detected._" not in comment
    assert "APPROVED" not in comment


def test_the_posted_comment_of_an_all_failed_review_says_failed():
    comment = _format_summary(_finished(agents_failed=list(ALL_AGENTS)), _MARKER)
    assert "REVIEW FAILED" in comment
    assert "_No issues detected._" not in comment
    assert "APPROVED" not in comment


def test_the_posted_comment_of_a_partial_review_says_partial():
    b = _finished(agents_run=["architect", "security"],
                  agents_failed=["quality"], findings=[_warning()])
    comment = _format_summary(b, _MARKER)
    assert "PARTIAL REVIEW" in comment
    # ...and still does its ordinary job below the notice.
    assert "Warning:** 1" in comment


def test_the_comment_and_the_row_carry_the_same_banner_word_for_word():
    """One source of truth. The fix was rejected in review once already for
    proposing a second wording composed inside `_format_summary`; the same
    `partial_banner` string has to appear in the comment, the summary the row
    stores, or in neither."""
    shapes = [
        dict(),                                                # nothing dispatched
        dict(agents_failed=list(ALL_AGENTS)),                  # everything failed
        dict(agents_run=["architect"], agents_failed=["quality"]),  # partial
        dict(agents_run=list(ALL_AGENTS)),                     # clean
    ]
    for kw in shapes:
        b = _finished(**kw)
        comment = _format_summary(b, _MARKER)
        if b.partial_banner:
            assert b.partial_banner.strip() in comment
            assert b.partial_banner.strip() in b.summary
        else:
            assert "⚠" not in comment


def test_no_issues_detected_still_means_someone_looked():
    """The sentence stays — for the review it is true of."""
    comment = _format_summary(_finished(agents_run=list(ALL_AGENTS)), _MARKER)
    assert "_No issues detected._" in comment
    assert "APPROVED" in comment
    assert "⚠" not in comment


def test_the_skipped_header_is_not_a_bare_speech_bubble():
    """SKIPPED fell through both verdict-map defaults and rendered as '💬 '
    — an empty claim line above '_No issues detected._'. The full header
    phrase is asserted, not just the word: the banner also says SKIPPED, so
    the word alone cannot tell a fixed header from a lucky substring."""
    comment = _format_summary(_finished(), _MARKER)
    assert "**SKIPPED** — nothing was reviewed" in comment
    assert "💬" not in comment
