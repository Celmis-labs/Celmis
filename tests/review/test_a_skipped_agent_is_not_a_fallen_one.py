"""`agents_skipped` reaches the row, so history can tell a choice from a crash.

Found by the night-bench harness, not by a user: its supervisor needed to
distinguish "contract is absent from agents_run because dispatch broke" from
"absent because the policy switched it off" — and could not, because
`agents_skipped` lived on the ReviewBatch object and died with the process.
`pragma table_info(review_runs)` showed only agents_run and agents_failed.

That is the class of failure this codebase keeps hunting: state that exists
in memory, is absent from storage, and reads from outside as something else.
An operator opening review history and seeing agents_run without `verifier`
had no way to learn the veto was disabled by policy rather than fallen over —
a decision reported as an outage.

Same nullability contract as its two siblings, pinned here the same way
theirs is: NULL is "written before the column existed", [] is "tracked, and
none skipped". Collapsing them would make every pre-migration row claim a
fully-dispatched review.
"""

from __future__ import annotations

import pytest

from src.api.review_runs import ReviewRunStore


@pytest.fixture
def store(tmp_path):
    return ReviewRunStore(db_path=tmp_path / "runs.sqlite3")


def _insert(store, run_id="r-1"):
    store.insert_queued(run_id, pr_ref="github:o/r#1", workspace_id="ws-1") \
        if hasattr(store, "insert_queued") else None
    return run_id


def test_the_roster_round_trips(store):
    from src.api.review_runs import ReviewRun

    store.insert(ReviewRun(id="r-1", pr_ref="github:o/r#1", user_id="u-1"))
    store.update("r-1", agents_run=["defect", "contract"],
                 agents_failed=[], agents_skipped=["verifier"])
    row = store.get("r-1")
    assert row.agents_skipped == ["verifier"]
    assert row.agents_run == ["defect", "contract"]


def test_an_empty_list_is_written_not_dropped(store):
    """[] is "tracked, none skipped" — the same rule agents_failed pins."""
    from src.api.review_runs import ReviewRun

    store.insert(ReviewRun(id="r-2", pr_ref="github:o/r#2", user_id="u-1"))
    store.update("r-2", agents_skipped=[])
    assert store.get("r-2").agents_skipped == []


def test_none_leaves_the_column_alone(store):
    from src.api.review_runs import ReviewRun

    store.insert(ReviewRun(id="r-3", pr_ref="github:o/r#3", user_id="u-1"))
    store.update("r-3", agents_skipped=["verifier"])
    store.update("r-3", summary="later update that says nothing about agents")
    assert store.get("r-3").agents_skipped == ["verifier"]


def test_a_pre_migration_row_reads_as_unknown(store):
    """NULL is not []. A row written before the column existed must not claim
    that nothing was skipped."""
    from src.api.review_runs import ReviewRun

    store.insert(ReviewRun(id="r-4", pr_ref="github:o/r#4", user_id="u-1"))
    assert store.get("r-4").agents_skipped is None


def test_the_wire_shape_carries_it():
    from src.api.schemas import ReviewRunOut

    assert "agents_skipped" in ReviewRunOut.model_fields
    assert ReviewRunOut.model_fields["agents_skipped"].default is None
