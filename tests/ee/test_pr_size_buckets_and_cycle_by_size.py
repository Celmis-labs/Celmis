# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Size is additions plus deletions; five buckets, each with its own cycle median."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.ee.analytics import productivity as m

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def merged(number: int, lines: tuple[int | None, int | None], *, hours: float = 10.0,
           detail: str = "full") -> dict:
    return {
        "number": number, "state": "merged", "detail_state": detail,
        "created_at": NOW - timedelta(hours=hours + 5), "first_commit_at": NOW - timedelta(hours=hours + 6),
        "first_review_at": NOW - timedelta(hours=hours + 1), "merged_at": NOW - timedelta(hours=1),
        "additions": lines[0], "deletions": lines[1],
    }


@pytest.mark.parametrize(("lines", "bucket"), [
    (0, "xs"), (10, "xs"), (11, "s"), (50, "s"), (51, "m"), (250, "m"),
    (251, "l"), (1000, "l"), (1001, "xl"), (50_000, "xl"),
])
def test_a_pr_lands_in_the_bucket_its_changed_lines_say(lines, bucket) -> None:
    assert m.size_bucket(lines) == bucket


def test_changed_lines_are_additions_plus_deletions_and_unknown_when_never_read() -> None:
    assert m.lines_changed({"additions": 7, "deletions": 3}) == 10
    assert m.lines_changed({"additions": 7, "deletions": None}) == 7
    assert m.lines_changed({"additions": None, "deletions": None}) is None


def test_the_histogram_counts_each_bucket_and_gives_its_median_cycle() -> None:
    prs = [merged(1, (3, 2), hours=4), merged(2, (4, 4), hours=8), merged(3, (400, 200), hours=40)]
    hist = {row["bucket"]: row for row in m.size_histogram(prs)}
    assert [row["bucket"] for row in m.size_histogram(prs)] == ["xs", "s", "m", "l", "xl"]
    assert hist["xs"]["count"] == 2 and hist["l"]["count"] == 1
    # Cycle runs from the first commit (hours + 6 ago) to the merge (1 hour ago).
    assert hist["xs"]["cycle_p50"] == 9 * 3600.0, "nearest rank of 9h and 13h"
    assert hist["l"]["cycle_p50"] == 45 * 3600.0
    assert hist["xl"]["count"] == 0 and hist["xl"]["cycle_p50"] is None


def test_a_pr_whose_diffstat_was_never_read_is_not_a_zero_line_pr() -> None:
    prs = [merged(1, (None, None)), merged(2, (5, 5), detail="none"), merged(3, (5, 5))]
    hist = m.size_histogram(prs)
    assert sum(row["count"] for row in hist) == 1


def test_the_median_pr_size_is_in_lines_and_has_its_previous_period() -> None:
    prs = [merged(1, (10, 0)), merged(2, (100, 0)), merged(3, (1000, 0))]
    out = m.overview(prs, [], days=7, now=NOW)
    assert out["kpis"]["pr_size"]["value"] == 100
    assert out["kpis"]["pr_size"]["previous"] is None
