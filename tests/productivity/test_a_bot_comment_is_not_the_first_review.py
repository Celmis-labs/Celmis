"""Celmis posts under a person's own account, so only its marker line tells it apart.

The pickup time of a PR is "until the first HUMAN review". A review comment
that Celmis wrote — under the account of the very person who then reviews —
must not start that clock; a human comment from the same account must.
"""

from __future__ import annotations

import pytest

from src.productivity import classify
from src.productivity.providers.base import PRDetail
from src.review import markers
from tests.productivity.support import (
    FakeProvider,
    approval,
    at,
    aware,
    comment,
    enable,
    make_engine,
    pr,
    prs_in,
    sync,
)

REVIEW_BODY = "Found 2 issues.\n\n<!-- code-analyzer:review -->"


def test_a_comment_with_the_review_marker_is_the_bots() -> None:
    assert classify.is_bot_comment(REVIEW_BODY)


def test_a_marker_hidden_for_bitbucket_is_still_the_bots() -> None:
    assert classify.is_bot_comment(markers.hide("Summary\n\n<!-- celmis:review-status:feedback -->"))


def test_a_human_comment_is_not_the_bots() -> None:
    assert not classify.is_bot_comment("Please rename this variable")


def test_a_quote_reply_that_copies_the_marker_is_still_a_humans() -> None:
    quoted = "> Found 2 issues.\n> <!-- code-analyzer:review -->\n\nI disagree with the second one."
    assert not classify.is_bot_comment(quoted)


def test_a_configured_bot_marker_counts_when_it_opens_a_line() -> None:
    assert classify.is_bot_comment("Review done\n<!-- kody-review -->", ["<!-- kody"])
    assert not classify.is_bot_comment("see <!-- kody-review --> in the docs", ["<!-- kody"])


@pytest.fixture
def engine():
    return make_engine()


def test_the_review_comment_of_the_same_account_does_not_start_the_pickup_clock(engine) -> None:
    enable(engine)
    detail = PRDetail(activity=[
        comment(1, "u-reviewer", at(-9, hours=1), raw=REVIEW_BODY),
        comment(2, "u-reviewer", at(-8), raw="Human here: this needs a test"),
    ])
    sync(engine, FakeProvider([pr(1, created=at(-9), merged=at(-5))], {1: detail}))
    row = prs_in(engine)[1]
    assert aware(row.first_review_at) == at(-8)
    assert row.human_comments == 1


def test_a_pr_reviewed_only_by_the_bot_has_no_human_review(engine) -> None:
    enable(engine)
    detail = PRDetail(activity=[comment(1, "u-reviewer", at(-9, hours=1), raw=REVIEW_BODY)])
    sync(engine, FakeProvider([pr(1, created=at(-9), merged=at(-5))], {1: detail}))
    row = prs_in(engine)[1]
    assert row.first_review_at is None
    assert row.human_comments == 0


def test_an_approval_is_a_review_and_counts_once(engine) -> None:
    enable(engine)
    detail = PRDetail(activity=[approval("u-reviewer", at(-7))])
    sync(engine, FakeProvider([pr(1, created=at(-9), merged=at(-5))], {1: detail}))
    row = prs_in(engine)[1]
    assert aware(row.first_review_at) == aware(row.first_approval_at) == at(-7)
    assert row.approvals == 1


def test_an_ignored_account_is_not_a_reviewer(engine) -> None:
    enable(engine, ignored_authors=["renovate"])
    detail = PRDetail(activity=[comment(1, "renovate", at(-8)), comment(2, "u-human", at(-6))])
    sync(engine, FakeProvider([pr(1, created=at(-9), merged=at(-5))], {1: detail}))
    assert aware(prs_in(engine)[1].first_review_at) == at(-6)
