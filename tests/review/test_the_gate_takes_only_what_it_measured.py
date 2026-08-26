"""The coverage gate refuses noise and nothing else, and the run says how much.

Two holes an adversarial pass found after the gate was built, both of the kind
this project keeps re-learning:

  * a pattern that matched NOTHING in the measured corpus was shipped anyway as
    an "inflection" of the ones that did — and it refuses "Backup code
    one-time consumption has untested race condition and replay vulnerability",
    a real benchmark sentence about a genuine time-of-check defect that merely
    uses the adjective. At this benchmark's operating point one true positive
    is worth eleven false positives, so a pattern that catches no measured
    noise and one real defect is a pure loss;
  * the refusal count reached `ReviewBatch` and stopped there. A run that
    silently stopped posting eight comments recorded the same hidden report as
    one that hid nothing — which is the exact failure the surrounding work
    exists to prevent.

The eight sentences below are verbatim from the measured run, not invented.
"""

from __future__ import annotations

import pytest

from src.api.review_runs import hidden_payload
from src.review.agents.base import reads_as_a_coverage_claim
from src.review.models import PullRequest, ReviewBatch

#: Every `*Why:*` sentence the gate refused in the measured run. All eight were
#: on the judge's false-positive list; none on any true-positive list.
MEASURED_NOISE = [
    "Missing unit tests for OptimizedCursorPaginator covering negative cursor offset branch",
    "No tests verify that Authenticate returns error when TagDevice returns ErrDeviceLimitReached",
    "No tests verify that updateDevice succeeds for existing devices when at capacity limit",
    "poll_feed method body is mocked in spec/jobs/poll_feed_spec.rb",
    "perform_retrieve implementation is completely mocked in spec/components/topic_retriever_spec.rb",
    "Missing test coverage for image downsizing logic in UploadsController#create_upload",
    "No automated tests verify disabling 2FA via backup code",
    "No tests verify 2FA backup code authentication flow in NextAuth options",
]

#: Real defects that mention testing vocabulary and MUST survive. The first is
#: the sentence the removed pattern refused.
MUST_SURVIVE = [
    "Backup code one-time consumption has untested race condition and replay vulnerability",
    "The assertion compares the wrong field, so a regression passes unnoticed",
    "The test asserts on a stale fixture, so the new branch is never exercised by CI "
    "and the wrong value ships",
    "TagDevice returns ErrDeviceLimitReached and Authenticate turns it into a 500",
]


@pytest.mark.parametrize("sentence", MEASURED_NOISE)
def test_every_measured_coverage_claim_is_refused(sentence):
    assert reads_as_a_coverage_claim(sentence) is not None


@pytest.mark.parametrize("sentence", MUST_SURVIVE)
def test_a_real_defect_survives_the_words_it_happens_to_use(sentence):
    assert reads_as_a_coverage_claim(sentence) is None, (
        "the gate refused a sentence describing a defect, which costs eleven "
        "times what refusing noise saves"
    )


def test_the_adjective_alone_is_not_a_coverage_claim():
    """`untested` on its own was shipped as an inflection and matched nothing in
    the corpus while refusing a golden. It must never come back without a
    negation near it."""
    assert reads_as_a_coverage_claim("an untested race condition in the retry path") is None
    assert reads_as_a_coverage_claim("this branch is not tested anywhere") is not None


# ─── and the run record says how many ────────────────────────────────


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7, title="t", description="",
        author="a", base_ref="main", base_sha="s", head_ref="f", head_sha="h",
        state="open",
    )


def test_the_refusal_count_reaches_the_run_record():
    """Eight comments a run stops posting must not read like nothing changed."""
    batch = ReviewBatch(pull_request=_pr(), dropped_coverage_claim=8)

    payload = hidden_payload(batch)

    assert payload["coverage_claim"] == 8


def test_a_run_that_refused_none_records_a_zero_not_a_gap():
    payload = hidden_payload(ReviewBatch(pull_request=_pr()))

    assert payload["coverage_claim"] == 0
    assert "no_evidence" in payload, "the sibling causes must still travel"


def test_the_wire_model_carries_it():
    from src.api.schemas import HiddenReportOut

    out = HiddenReportOut(**hidden_payload(ReviewBatch(pull_request=_pr(),
                                                       dropped_coverage_claim=3)))

    assert out.coverage_claim == 3


# ─── the window that cost seven true positives ──────────────────────

#: Sentences in which "test" means a COMPARISON, not a unit test. These are
#: ordinary quality-agent prose. A six-word window between the negation and
#: the word "test" catches every one of them; three words catches none. The
#: benchmark run that shipped the six-word window saw the quality agent fall
#: from 9 true positives to 2 and F2 fall 5.71 points against a noise floor of
#: 1.78 — the single most expensive line changed in this project.
NOT_ABOUT_TESTING = [
    "no null check is performed before the test for emptiness",
    "the loop has no guard, so the emptiness test on line 40 runs on nil",
    "without a length check the boundary test on line 12 reads past the end",
    "missing an early return, the equality test compares undefined values",
]


@pytest.mark.parametrize("sentence", NOT_ABOUT_TESTING)
def test_a_comparison_called_a_test_is_not_a_coverage_claim(sentence):
    assert reads_as_a_coverage_claim(sentence) is None, (
        "the gate refused a defect because its sentence used the word 'test' "
        "to mean a comparison; this is what the six-word window did"
    )


def test_the_window_stays_narrow_enough_to_tell_them_apart():
    """The boundary itself, stated as behaviour rather than as a number: a
    negation FAR from the word `test` is prose about something else, and a
    negation NEXT TO it is a coverage claim."""
    assert reads_as_a_coverage_claim("no test covers this branch") is not None
    assert reads_as_a_coverage_claim("no unit test covers this branch") is not None
    # Six words away — the widened window's reach, and prose the gate must not touch.
    assert reads_as_a_coverage_claim(
        "no guard exists on the path that reaches the equality test below"
    ) is None
