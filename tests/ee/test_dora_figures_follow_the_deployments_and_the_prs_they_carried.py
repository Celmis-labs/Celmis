# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Deploy frequency, lead time, change failure rate and recovery time.

The rules that are easy to get wrong: a deployment still inside its failure
window is in neither side of the failure rate; lead time counts the PRs
deployed in the window, not merged in it; PRs merged but not yet shipped are
reported on their own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.ee.analytics import productivity as m

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def days_ago(d: float) -> datetime:
    return NOW - timedelta(days=d)


def dep(days: float, *, failed=False, recovered_days: float | None = None, settle=7) -> dict:
    return {
        "id": f"d{days}", "repo": "acme/shop", "deployed_at": days_ago(days), "source": "merge",
        "is_failure": failed, "settle_days": settle,
        "recovered_at": days_ago(recovered_days) if recovered_days is not None else None,
    }


def shipped(number: int, *, first_commit: float, deployed: float | None, merged: float = 5.0,
            kind: str = "feature") -> dict:
    return {
        "number": number, "state": "merged", "kind": kind, "detail_state": "full",
        "created_at": days_ago(first_commit - 0.5), "first_commit_at": days_ago(first_commit),
        "merged_at": days_ago(merged),
        "prod_deployed_at": days_ago(deployed) if deployed is not None else None,
    }


def test_deploys_per_week_is_the_count_over_the_weeks_in_the_window() -> None:
    deps = [dep(d) for d in (1, 3, 5, 8, 10, 12)]
    out = m.overview([], deps, days=14, now=NOW)["kpis"]["deploy_frequency"]
    assert out["total"] == 6
    assert out["value"] == pytest.approx(3.0)
    assert out["per_day"] == pytest.approx(6 / 14)


def test_deployments_outside_the_window_are_the_previous_period_not_this_one() -> None:
    deps = [dep(1), dep(2), dep(20), dep(21), dep(22), dep(23)]
    out = m.overview([], deps, days=14, now=NOW)["kpis"]["deploy_frequency"]
    assert out["total"] == 2
    assert out["previous"] == pytest.approx(4 / 14 * 7)
    assert out["delta"] == pytest.approx(-0.5)


def test_a_deployment_still_inside_its_failure_window_is_not_counted_either_way() -> None:
    deps = [dep(30), dep(20, failed=True), dep(15), dep(10), dep(2, failed=True)]
    out = m.overview([], deps, days=60, now=NOW)["kpis"]["change_failure_rate"]
    assert out["settled"] == 4 and out["unsettled"] == 1
    assert out["failed"] == 1, "the failure two days ago may still be a good deploy tomorrow"
    assert out["value"] == pytest.approx(0.25)


def test_each_repository_settles_on_its_own_failure_window() -> None:
    deps = [dep(10, failed=True, settle=14), dep(10, settle=3)]
    out = m.overview([], deps, days=30, now=NOW)["kpis"]["change_failure_rate"]
    assert out["settled"] == 1 and out["unsettled"] == 1


def test_with_no_settled_deployment_there_is_no_failure_rate_to_report() -> None:
    out = m.overview([], [dep(1)], days=30, now=NOW)["kpis"]["change_failure_rate"]
    assert out["value"] is None and out["band"] is None


def test_recovery_time_is_the_median_from_a_failed_deploy_to_the_fix() -> None:
    deps = [dep(20, failed=True, recovered_days=19.5), dep(15, failed=True, recovered_days=14),
            dep(10, failed=True), dep(9)]
    out = m.overview([], deps, days=60, now=NOW)["kpis"]["time_to_recover"]
    assert out["n"] == 2, "a failure not yet recovered has no recovery time"
    assert out["value"] == pytest.approx(0.5 * 86400)


def test_lead_time_counts_the_prs_deployed_in_the_window_from_their_first_commit() -> None:
    prs = [shipped(1, first_commit=12, deployed=10), shipped(2, first_commit=20, deployed=6),
           shipped(3, first_commit=40, deployed=35, merged=36)]
    out = m.overview(prs, [], days=14, now=NOW)["kpis"]["lead_time"]
    assert out["n"] == 2
    assert out["value"] == pytest.approx(2 * 86400), "nearest rank of 2 days and 14 days"
    assert out["band"] == "high"


def test_merged_prs_that_never_reached_production_are_reported_not_hidden() -> None:
    prs = [shipped(1, first_commit=12, deployed=3), shipped(2, first_commit=9, deployed=None)]
    out = m.overview(prs, [], days=14, now=NOW)["kpis"]["lead_time"]
    assert out["undeployed_merged"] == 1


def test_the_bug_ratio_is_the_share_of_merged_prs_that_fix_something() -> None:
    prs = [shipped(1, first_commit=9, deployed=1, kind="feature"),
           shipped(2, first_commit=9, deployed=1, kind="bugfix"),
           shipped(3, first_commit=9, deployed=1, kind="hotfix"),
           shipped(4, first_commit=9, deployed=1, kind="revert")]
    assert m.overview(prs, [], days=14, now=NOW)["kpis"]["bug_ratio"]["value"] == pytest.approx(0.75)


def test_the_timeline_flags_failed_and_unsettled_deployments_oldest_first() -> None:
    rows = m.overview([], [dep(1), dep(12, failed=True)], days=14, now=NOW)["deployments"]
    assert [r["failed"] for r in rows] == [True, False]
    assert [r["settled"] for r in rows] == [True, False]


@pytest.mark.parametrize(("per_day", "band"), [(2, "elite"), (1, "elite"), (0.2, "high"),
                                              (0.05, "medium"), (0.01, "low"), (None, None)])
def test_deploy_frequency_is_placed_in_a_dora_band(per_day, band) -> None:
    assert m.band_deploy_frequency(per_day) == band


@pytest.mark.parametrize(("seconds", "band"), [(3600, "elite"), (2 * 86400, "high"),
                                              (10 * 86400, "medium"), (60 * 86400, "low")])
def test_lead_time_is_placed_in_a_dora_band(seconds, band) -> None:
    assert m.band_lead_time(seconds) == band


@pytest.mark.parametrize(("rate", "band"), [(0.0, "elite"), (0.08, "high"), (0.12, "medium"), (0.4, "low")])
def test_the_failure_rate_is_placed_in_a_dora_band(rate, band) -> None:
    assert m.band_failure_rate(rate) == band
