"""The engine's guard rails: one run per repository, a bad PR does not stop the rest, errors are kept and sentences."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from src.db.models import ProductivitySyncState
from src.productivity import sync as ps
from src.productivity.providers.base import ProviderError
from tests.productivity.support import (
    NOW,
    PROVIDER,
    REPO,
    WS,
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


def test_a_second_run_while_the_first_holds_the_lease_does_nothing() -> None:
    engine = make_engine()
    enable(engine)
    sync(engine, FakeProvider([pr(1, merged=at(-3))]))
    with Session(engine) as s:
        s.get(ProductivitySyncState, (WS, PROVIDER, REPO)).running_until = NOW + timedelta(minutes=5)
        s.commit()
    rival = FakeProvider([pr(2, merged=at(-2))])
    assert sync(engine, rival).status == "busy"
    assert rival.list_since == []


def test_a_lease_that_ran_out_is_taken_over() -> None:
    engine = make_engine()
    enable(engine)
    sync(engine, FakeProvider([pr(1, merged=at(-3))]))
    with Session(engine) as s:
        s.get(ProductivitySyncState, (WS, PROVIDER, REPO)).running_until = NOW - timedelta(minutes=5)
        s.commit()
    assert sync(engine, FakeProvider([pr(2, merged=at(-2))])).status == "ok"


def test_the_lease_is_released_when_the_run_ends_even_by_error() -> None:
    engine = make_engine()
    enable(engine)

    def broken(cfg):
        raise ProviderError("no bitbucket credential saved for workspace ws-1")

    result = ps.run_repo_sync(WS, PROVIDER, REPO, engine=engine, now=NOW, provider_factory=broken)
    assert result.status == "error"
    state = state_of(engine)
    assert state.running_until is None
    assert state.last_error == "ProviderError: no bitbucket credential saved for workspace ws-1"


def test_a_pr_whose_detail_fails_does_not_stop_the_others_and_is_named() -> None:
    engine = make_engine()
    enable(engine)

    class Picky(FakeProvider):
        def pr_detail(self, record):
            if record.number == 2:
                raise ProviderError("bitbucket: HTTP 404 for /pullrequests/2/activity")
            return super().pr_detail(record)

    provider = Picky([pr(n, merged=at(-30 + n), created=at(-33 + n)) for n in (1, 2, 3)])
    result = sync(engine, provider)
    assert provider.detail_calls == [3, 1]
    assert result.status == "ok" and "1 PR" in (result.error or "")
    assert {n: r.detail_state for n, r in prs_in(engine).items()} == {1: "full", 2: "none", 3: "full"}
    assert result.more_work is True or result.status == "ok"


def test_a_credential_that_fails_every_pr_stops_the_run_after_a_few_not_after_all_of_them() -> None:
    engine = make_engine()
    enable(engine)

    class Revoked(FakeProvider):
        def pr_detail(self, record):
            self.detail_calls.append(record.number)
            raise ProviderError("bitbucket: HTTP 401 for /pullrequests/1/commits")

    provider = Revoked([pr(n, merged=at(-100 + n), created=at(-101 + n)) for n in range(1, 60)])
    result = sync(engine, provider)
    assert result.status == "error" and "401" in result.error
    assert len(provider.detail_calls) == 10


def test_an_empty_budget_stops_before_any_work_and_asks_to_be_continued() -> None:
    engine = make_engine()
    enable(engine)
    provider = FakeProvider([pr(1, merged=at(-3))])
    result = sync(engine, provider, time_budget=0.0)
    assert result.status == "budget" and result.more_work
    assert provider.detail_calls == []


def test_a_cancel_request_ends_the_run_and_says_so() -> None:
    engine = make_engine()
    enable(engine)
    provider = FakeProvider([pr(n, merged=at(-30 + n), created=at(-33 + n)) for n in range(1, 5)])
    result = sync(engine, provider, cancel_check=lambda: True)
    assert result.status == "cancelled" and provider.detail_calls == []
    assert state_of(engine).running_until is None


def test_a_full_resync_forgets_the_watermark_and_details_everything_again() -> None:
    engine = make_engine()
    enable(engine)
    prs = [pr(1, merged=at(-5)), pr(2, merged=at(-4))]
    sync(engine, FakeProvider(prs))
    again = FakeProvider(prs)
    sync(engine, again, full=True)
    assert again.list_since == [NOW - timedelta(days=180)]
    assert sorted(again.detail_calls) == [1, 2]


def test_progress_is_counted_for_the_page_that_shows_it() -> None:
    engine = make_engine()
    enable(engine)
    sync(engine, FakeProvider([pr(n, merged=at(-30 + n), created=at(-33 + n)) for n in range(1, 5)],
                              limit_at=1))
    state = state_of(engine)
    assert (state.prs_total, state.prs_detailed, state.prs_pending) == (4, 1, 3)
    rows = ps.sync_status(WS, engine=engine)
    assert rows[0]["repo"] == REPO and rows[0]["prs_pending"] == 3 and rows[0]["rate_limited_until"]


def test_a_merged_pr_gets_its_exact_merge_time_from_the_detail() -> None:
    from src.productivity.providers.base import PRDetail

    engine = make_engine()
    enable(engine)
    listed = pr(1, merged=at(-3), updated=at(-2))
    listed.merged_at_approx = True
    exact = at(-3, hours=-5)
    sync(engine, FakeProvider([listed], {1: PRDetail(merged_at=exact, first_commit_at=at(-9), commits_count=3)}))
    row = prs_in(engine)[1]
    assert aware(row.merged_at) == exact and row.merged_at_approx is False


def test_an_approximate_merge_time_is_not_allowed_to_overwrite_an_exact_one() -> None:
    from src.productivity.providers.base import PRDetail

    engine = make_engine()
    enable(engine)
    listed = pr(1, merged=at(-3), updated=at(-2))
    listed.merged_at_approx = True
    exact = at(-3, hours=-5)
    sync(engine, FakeProvider([listed], {1: PRDetail(merged_at=exact)}), now=at(-1))
    again = pr(1, merged=at(-1, hours=-1), updated=at(-1, hours=-1))
    again.merged_at_approx = True
    sync(engine, FakeProvider([again]), now=at(0))
    assert aware(prs_in(engine)[1].merged_at) == exact


def test_a_backfill_estimate_says_how_many_calls_and_how_long() -> None:
    provider = FakeProvider([pr(n, merged=at(-5)) for n in range(1, 101)])
    provider.requests_per_pr, provider.page_size = 4, 50
    got = ps.estimate_backfill(provider, 180, rate_per_hour=500, now=NOW)
    assert got == {"prs": 100, "requests": 402, "hours": 0.8}


def test_a_provider_that_cannot_count_gives_no_estimate_rather_than_a_guess() -> None:
    provider = FakeProvider([])
    provider.count_pull_requests = lambda since: None
    assert ps.estimate_backfill(provider, 30)["prs"] is None


def test_building_a_provider_without_a_saved_credential_says_which_workspace(monkeypatch) -> None:
    monkeypatch.setattr("src.credentials.resolve_git_credential", lambda *a, **kw: None)
    with pytest.raises(ProviderError, match="ws-9"):
        ps.build_provider("ws-9", "bitbucket", "acme/app")


def test_a_merge_refresh_waits_while_a_sync_holds_the_repository_and_then_releases_it() -> None:
    engine = make_engine()
    enable(engine, production_branches=["master"])
    sync(engine, FakeProvider([pr(7, target="master", merged=at(-1), merge_sha="m7")]))
    with Session(engine) as s:
        s.get(ProductivitySyncState, (WS, PROVIDER, REPO)).running_until = NOW + timedelta(minutes=5)
        s.commit()
    rival = FakeProvider([pr(7, target="master", merged=at(-1), merge_sha="m7")])
    busy = ps.refresh_pull_request(WS, PROVIDER, REPO, 7, provider_factory=lambda cfg: rival,
                                   engine=engine, now=NOW)
    assert busy.status == "busy" and busy.more_work is True and rival.detail_calls == []
    assert aware(state_of(engine).running_until) == NOW + timedelta(minutes=5)   # not stolen, not released
    with Session(engine) as s:
        s.get(ProductivitySyncState, (WS, PROVIDER, REPO)).running_until = None
        s.commit()
    done = ps.refresh_pull_request(WS, PROVIDER, REPO, 7, provider_factory=lambda cfg: rival,
                                   engine=engine, now=NOW)
    assert done.status == "ok" and rival.detail_calls == [7]
    assert state_of(engine).running_until is None
