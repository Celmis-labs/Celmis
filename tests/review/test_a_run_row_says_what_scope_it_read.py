"""The run row records whether a review read the whole pull request or an increment.

`scope` ('full' | 'incremental') and `scope_base_sha` ride `pr_snapshot`, the
one helper both run writers use. A row written before the columns reads None
("not recorded"), never "full". An incremental run keeps the WHOLE pull
request's diff in the row, because its findings are anchored on it.
"""

from __future__ import annotations

import sqlite3

from src.api.review_runs import ReviewRun, ReviewRunStore, pr_snapshot
from src.review.models import PullRequest, ReviewBatch, ScopeInfo


def _batch(*, scope=None, mode=None, raw="increment diff") -> ReviewBatch:
    pr = PullRequest(
        provider="github", repo="acme/shop", number=7, title="t", description="",
        author="u", base_ref="develop", base_sha="b", head_ref="feat", head_sha="h" * 12,
        state="open", raw_diff=raw, scope=scope)
    batch = ReviewBatch(pull_request=pr)
    batch.scope_mode = mode
    return batch


def test_a_whole_review_is_recorded_as_full() -> None:
    snap = pr_snapshot(_batch(mode="full", raw="whole diff"))

    assert snap["scope"] == "full" and snap["scope_base_sha"] is None
    assert snap["raw_diff"] == "whole diff"


def test_an_incremental_review_records_its_base_and_keeps_the_whole_diff() -> None:
    scope = ScopeInfo(base_sha="a" * 40, new_commits=2, full_raw_diff="the whole pull request")

    snap = pr_snapshot(_batch(scope=scope, mode="incremental"))

    assert snap["scope"] == "incremental" and snap["scope_base_sha"] == "a" * 40
    assert snap["raw_diff"] == "the whole pull request"


def test_a_run_that_ended_before_the_scope_stage_records_no_scope() -> None:
    snap = pr_snapshot(_batch(mode=None))

    assert snap["scope"] is None


def test_the_scope_survives_a_round_trip_through_the_store(tmp_path) -> None:
    store = ReviewRunStore(tmp_path / "runs.db")
    store.insert(ReviewRun(id="r1", user_id="u", pr_ref="github:acme/shop#7"))

    store.update("r1", scope="incremental", scope_base_sha="a" * 40)

    row = store.get("r1")
    assert (row.scope, row.scope_base_sha) == ("incremental", "a" * 40)


def test_a_row_from_before_the_columns_reads_not_recorded(tmp_path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE review_runs (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, pr_ref TEXT NOT NULL,
                status TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT 'pending',
                findings_count INTEGER NOT NULL DEFAULT 0,
                critical INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                warning INTEGER NOT NULL DEFAULT 0, info INTEGER NOT NULL DEFAULT 0,
                cross_repo_callers INTEGER NOT NULL DEFAULT 0,
                posted INTEGER NOT NULL DEFAULT 0, elapsed_seconds REAL,
                summary TEXT NOT NULL DEFAULT '', error_message TEXT,
                started_at TEXT NOT NULL, finished_at TEXT);
            INSERT INTO review_runs (id, user_id, pr_ref, status, started_at)
            VALUES ('old', 'u', 'github:o/r#1', 'complete', '2026-01-01T00:00:00+00:00');
        """)

    store = ReviewRunStore(db)
    ReviewRunStore(db)  # idempotent

    assert store.get("old").scope is None
