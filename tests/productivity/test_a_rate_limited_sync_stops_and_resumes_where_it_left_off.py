"""A 429 ends the run, keeps the cursor, and the next run does not start before it is allowed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.productivity.ratelimit import RateGate, RateLimited, parse_retry_after
from tests.productivity.support import (
    NOW,
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


def _prs():
    return [pr(n, merged=at(-30 + n), created=at(-33 + n)) for n in range(1, 5)]


def test_a_limit_mid_detail_keeps_what_was_done_and_records_when_to_come_back() -> None:
    engine = make_engine()
    enable(engine)
    until = NOW + timedelta(minutes=45)
    limited = FakeProvider(_prs(), limit_at=2, limit_until=until)
    result = sync(engine, limited)
    assert result.status == "rate_limited" and result.more_work
    assert aware(result.resume_at) == until
    state = state_of(engine)
    assert aware(state.rate_limited_until) == until
    assert state.prs_total == 4 and state.prs_detailed == 2 and state.prs_pending == 2
    assert limited.detail_calls == [4, 3]


def test_a_run_before_the_limit_ends_does_not_touch_the_provider() -> None:
    engine = make_engine()
    enable(engine)
    until = NOW + timedelta(minutes=45)
    sync(engine, FakeProvider(_prs(), limit_at=1, limit_until=until))
    built = []

    from src.productivity.sync import run_repo_sync

    result = run_repo_sync("ws-1", "bitbucket", "acme/app", engine=engine, now=NOW + timedelta(minutes=10),
                           provider_factory=lambda cfg: built.append(cfg))
    assert result.status == "rate_limited" and aware(result.resume_at) == until
    assert built == []


def test_the_run_after_the_limit_finishes_the_rest_and_repeats_nothing() -> None:
    engine = make_engine()
    enable(engine)
    until = NOW + timedelta(minutes=45)
    sync(engine, FakeProvider(_prs(), limit_at=2, limit_until=until))
    rest = FakeProvider(_prs())
    result = sync(engine, rest, now=NOW + timedelta(hours=1))
    assert result.status == "ok"
    assert rest.detail_calls == [2, 1]
    assert state_of(engine).rate_limited_until is None
    assert all(r.detail_state == "full" for r in prs_in(engine).values())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _gate(rate: int = 360, burst: int = 2):
    clock = Clock()
    return RateGate(rate, burst=burst, clock=clock, sleep=clock.sleep), clock


def test_the_bucket_lets_a_burst_through_and_then_paces_calls() -> None:
    gate, clock = _gate()           # 360/h = one token every 10 seconds
    gate.acquire()
    gate.acquire()
    assert clock.slept == []
    gate.acquire()
    assert clock.slept == [pytest.approx(10.0)]


def test_a_wait_longer_than_the_allowance_is_refused_not_slept() -> None:
    gate, clock = _gate()
    gate.acquire()
    gate.acquire()
    with pytest.raises(RateLimited) as caught:
        gate.acquire(max_wait=5.0)
    assert clock.slept == []
    assert caught.value.until > datetime.now(UTC)


def test_a_429_blocks_every_caller_until_retry_after_has_passed() -> None:
    gate, clock = _gate(rate=3600, burst=10)
    gate.penalise(120)
    with pytest.raises(RateLimited):
        gate.acquire(max_wait=30)
    gate.acquire(max_wait=300)
    assert clock.slept and clock.slept[0] == pytest.approx(120, abs=1)


def test_retry_after_reads_seconds_dates_and_garbage() -> None:
    assert parse_retry_after("90") == 90
    assert parse_retry_after(None) == 60
    assert parse_retry_after("soon") == 60
    assert parse_retry_after("0") == 1
    assert parse_retry_after("999999999") == 86400
