"""The watermark: a second run reads from where the first one stopped, and keeps what it has."""

from __future__ import annotations

from tests.productivity.support import (
    FakeProvider,
    at,
    aware,
    enable,
    make_engine,
    pr,
    prs_in,
    state_of,
    sync,
)


def _first_three():
    return [pr(1, merged=at(-30), created=at(-33)), pr(2, merged=at(-20), created=at(-22)),
            pr(3, merged=at(-10), created=at(-12))]


def test_the_first_run_moves_the_watermark_to_the_newest_update() -> None:
    engine = make_engine()
    enable(engine)
    sync(engine, FakeProvider(_first_three()))
    assert aware(state_of(engine).updated_watermark) == at(-10)
    assert len(prs_in(engine)) == 3


def test_a_second_run_asks_from_the_watermark_and_detail_is_not_repeated() -> None:
    engine = make_engine()
    enable(engine)
    first = FakeProvider(_first_three())
    sync(engine, first)
    second = FakeProvider(_first_three() + [pr(4, merged=at(-1), created=at(-3))])
    sync(engine, second, now=at(0, hours=1))
    assert second.list_since == [at(-10)]
    # PR 3 sits ON the watermark and is listed again, but it is closed and already detailed.
    assert second.detail_calls == [4]
    assert sorted(prs_in(engine)) == [1, 2, 3, 4]
    assert aware(state_of(engine).updated_watermark) == at(-1)


def test_an_open_pr_that_was_updated_is_detailed_again_and_a_merged_one_is_not() -> None:
    engine = make_engine()
    enable(engine)
    sync(engine, FakeProvider([pr(5, state="open", created=at(-5), updated=at(-4))]), now=at(-3))
    again = FakeProvider([pr(5, state="open", created=at(-5), updated=at(-1))])
    sync(engine, again, now=at(0))
    assert again.detail_calls == [5]
    merged = FakeProvider([pr(5, state="merged", created=at(-5), merged=at(0, hours=-2), updated=at(0, hours=-2))])
    sync(engine, merged, now=at(0, hours=1))
    assert merged.detail_calls == [5]            # open -> merged is a state change
    nothing = FakeProvider([pr(5, state="merged", created=at(-5), merged=at(0, hours=-2), updated=at(0, hours=-2))])
    sync(engine, nothing, now=at(0, hours=2))
    assert nothing.detail_calls == []            # merged and unchanged: left alone


def test_the_rows_carry_the_providers_creation_time_not_the_time_they_were_seen() -> None:
    engine = make_engine()
    enable(engine)
    sync(engine, FakeProvider([pr(1, created=at(-33), merged=at(-30))]))
    assert aware(prs_in(engine)[1].created_at) == at(-33)


def test_the_newest_pr_is_detailed_first_so_the_dashboard_fills_from_today() -> None:
    engine = make_engine()
    enable(engine)
    provider = FakeProvider(_first_three())
    sync(engine, provider)
    assert provider.detail_calls == [3, 2, 1]
