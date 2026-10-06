# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""A merged PR's cycle time is coding, then waiting for a first review, then review.

Pure functions over row dicts (src/ee/analytics/productivity.py): no database,
no clock but the `now` the test passes in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.ee.analytics import productivity as m

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = 3600.0


def at(hours: float) -> datetime:
    """`hours` before NOW."""
    return NOW - timedelta(hours=hours)


def pr(number=1, *, first_commit=30.0, created=24.0, review=20.0, merged=10.0,
       detail="full", state="merged", **extra) -> dict:
    return {
        "number": number, "state": state, "detail_state": detail,
        "first_commit_at": at(first_commit) if first_commit is not None else None,
        "created_at": at(created), "first_review_at": at(review) if review is not None else None,
        "merged_at": at(merged) if state == "merged" else None, **extra,
    }


def test_the_three_parts_add_up_to_the_cycle() -> None:
    parts = m.cycle_parts(pr())
    assert parts["coding"] == 6 * HOUR
    assert parts["pickup"] == 4 * HOUR
    assert parts["review"] == 10 * HOUR
    assert parts["cycle"] == 20 * HOUR
    assert parts["coding"] + parts["pickup"] + parts["review"] == parts["cycle"]


def test_a_commit_dated_after_the_pr_was_opened_does_not_give_negative_coding_time() -> None:
    parts = m.cycle_parts(pr(first_commit=20.0, created=24.0))
    assert parts["coding"] == 0.0


def test_a_review_before_the_pr_was_opened_has_no_negative_pickup() -> None:
    assert m.cycle_parts(pr(review=30.0))["pickup"] == 0.0


def test_a_comment_after_the_merge_is_not_a_review_of_the_merge() -> None:
    """The PR shipped unreviewed; a later remark must not invent a review phase."""
    parts = m.cycle_parts(pr(review=5.0, merged=10.0))
    assert parts["reviewed"] is False
    assert parts["pickup"] is None and parts["review"] is None


def test_a_pr_without_a_known_first_commit_starts_its_cycle_when_it_was_opened() -> None:
    parts = m.cycle_parts(pr(first_commit=None))
    assert parts["coding"] is None
    assert parts["cycle"] == 14 * HOUR


def test_a_pr_nobody_has_read_yet_is_unknown_not_unreviewed() -> None:
    parts = m.cycle_parts(pr(detail="none", review=None))
    assert parts["known"] is False
    assert parts["pickup"] is None and parts["review"] is None and parts["reviewed"] is False


def test_medians_p75_and_p90_are_nearest_rank_and_never_means() -> None:
    prs = [pr(i, review=20.0, merged=float(10 - i)) for i in range(1, 6)]
    figures = m.window_figures(prs, [], NOW - timedelta(days=2), NOW, now=NOW)
    cycle = figures["cycle"]
    assert cycle["n"] == 5
    assert cycle["p50"] == sorted(m.cycle_parts(p)["cycle"] for p in prs)[2]
    assert cycle["p90"] == max(m.cycle_parts(p)["cycle"] for p in prs)


def test_a_pr_merged_without_a_human_review_is_counted_and_left_out_of_pickup_and_review() -> None:
    prs = [pr(1), pr(2, review=None), pr(3, detail="none", review=None)]
    figures = m.window_figures(prs, [], NOW - timedelta(days=2), NOW, now=NOW)
    assert figures["merged"] == 3
    assert figures["merged_without_review"] == 1, "the unread PR is not claimed to be unreviewed"
    assert figures["not_enriched"] == 1
    assert figures["pickup"]["n"] == 1 and figures["review"]["n"] == 1
    assert figures["cycle"]["n"] == 2, "an unread PR has no cycle time: its merge time may be approximate"


def test_only_prs_merged_in_the_window_have_a_cycle_time() -> None:
    old = pr(1, merged=24 * 40.0, created=24 * 40.0 + 5, first_commit=24 * 40.0 + 6, review=24 * 40.0 + 2)
    figures = m.window_figures([old, pr(2)], [], NOW - timedelta(days=30), NOW, now=NOW)
    assert figures["merged"] == 1


def test_the_overview_carries_the_previous_period_and_the_change_against_it() -> None:
    this = [pr(i, merged=10.0 + i) for i in range(1, 4)]
    before = [
        pr(10 + i, first_commit=24 * 3 + 40.0, created=24 * 3 + 30.0, review=24 * 3 + 20.0,
           merged=24 * 3 + 10.0 + i)
        for i in range(1, 3)
    ]
    out = m.overview(this + before, [], days=3, now=NOW)
    merged = out["kpis"]["merged_prs"]
    assert merged["value"] == 3 and merged["previous"] == 2
    assert merged["delta"] == pytest.approx(0.5)
    assert out["kpis"]["cycle_time"]["previous"] is not None


def test_a_change_against_nothing_is_not_a_percentage() -> None:
    out = m.overview([pr(1)], [], days=3, now=NOW)
    assert out["kpis"]["merged_prs"]["previous"] == 0
    assert out["kpis"]["merged_prs"]["delta"] is None


def test_the_weekly_series_has_a_bucket_for_every_week_and_stacks_the_medians() -> None:
    out = m.overview([pr(1)], [], days=30, bucket="week", now=NOW)
    cycle = out["series"]["cycle"]
    assert len(cycle) in (5, 6)
    assert all(row["date"] <= NOW.date().isoformat() for row in cycle)
    filled = [row for row in cycle if row["n"]]
    assert len(filled) == 1
    assert (filled[0]["coding"], filled[0]["pickup"], filled[0]["review"]) == (6 * HOUR, 4 * HOUR, 10 * HOUR)
    assert all(datetime.fromisoformat(row["date"]).weekday() == 0 for row in cycle), "weeks start on Monday"


def test_a_pr_nobody_has_read_yet_is_left_out_of_cycle_time_and_size_cycle_medians() -> None:
    """Its merge time can be the list's last-update time, so its cycle is another definition."""
    unread = pr(7, detail="none", review=None, first_commit=None)
    assert m.cycle_parts(unread)["cycle"] is None
    figures = m.window_figures([pr(1), unread], [], NOW - timedelta(days=2), NOW, now=NOW)
    assert figures["cycle"]["n"] == 1
    assert figures["merged"] == 2 and figures["not_enriched"] == 1
