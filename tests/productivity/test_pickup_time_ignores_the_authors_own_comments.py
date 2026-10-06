"""Pickup is the wait for SOMEONE ELSE. An author talking to themselves is not a review."""

from __future__ import annotations

from src.productivity.providers.base import PRDetail
from tests.productivity.support import (
    FakeProvider,
    at,
    aware,
    comment,
    enable,
    make_engine,
    pr,
    prs_in,
    sync,
)


def test_the_authors_own_comment_does_not_end_the_wait() -> None:
    engine = make_engine()
    enable(engine)
    detail = PRDetail(activity=[
        comment(1, "u-author", at(-9, hours=2), raw="Pushed a fix"),
        comment(2, "u-other", at(-7), raw="Looks good"),
    ])
    sync(engine, FakeProvider([pr(1, author="u-author", created=at(-9), merged=at(-5))], {1: detail}))
    row = prs_in(engine)[1]
    assert aware(row.first_review_at) == at(-7)
    assert row.human_comments == 1


def test_a_pr_nobody_else_touched_has_no_review_at_all() -> None:
    engine = make_engine()
    enable(engine)
    detail = PRDetail(activity=[comment(1, "u-author", at(-9, hours=2))])
    sync(engine, FakeProvider([pr(1, author="u-author", created=at(-9), merged=at(-5))], {1: detail}))
    assert prs_in(engine)[1].first_review_at is None
