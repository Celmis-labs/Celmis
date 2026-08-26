"""A review that lost a stage has to still be saying so tomorrow.

The pipeline already knew. `agents_failed` was populated, `compute_verdict`
refused to APPROVE on a critical agent's absence, and `mark_complete` stopped
undoing that. But the knowledge lived for the length of one function call: it
reached the user as a prose prefix glued onto the front of `summary`, and the
run row said `status = 'complete'`.

So once the pull-request comment had been read, nothing could answer "which
agent failed?" — not the reviews page, not an API consumer, not a re-run that
would like to retry only the stage that broke. The product's own history said
the review had completed.

Three things are pinned here:

  * `partial` is a member of the status vocabulary the run row already used,
    not a second flag beside it (Kodus calls the same state PARTIAL_ERROR:
    every comment posted, one stage missing).
  * the banner in the summary and the columns in the row are derived from the
    same `agents_failed` list, so they cannot name different agents.
  * NULL is not []. A row written before the columns existed reads back as
    "unknown", never as "nothing failed" — the false negative this whole wave
    is about, moved from the verdict into the history.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.api.review_runs import ReviewRun, ReviewRunStore
from src.review.models import (
    Finding,
    FindingSeverity,
    Hunk,
    PullRequest,
    ReviewBatch,
    ReviewRunStatus,
    ReviewVerdict,
)

# The columns as an older deploy left them: everything up to the workspace
# migration, and nothing about agents. Written out rather than derived from
# _SCHEMA so that editing _SCHEMA cannot quietly make the "legacy" fixture
# modern and retire the test without anyone noticing.
_LEGACY_SCHEMA = """
CREATE TABLE review_runs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    pr_ref          TEXT NOT NULL,
    status          TEXT NOT NULL,
    verdict         TEXT NOT NULL DEFAULT 'pending',
    findings_count  INTEGER NOT NULL DEFAULT 0,
    critical        INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    warning         INTEGER NOT NULL DEFAULT 0,
    info            INTEGER NOT NULL DEFAULT 0,
    cross_repo_callers INTEGER NOT NULL DEFAULT 0,
    posted          INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds REAL,
    summary         TEXT NOT NULL DEFAULT '',
    error_message   TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT
);
"""


def _pr() -> PullRequest:
    return PullRequest(
        provider="github", repo="acme/api", number=7,
        title="t", description="d", author="alice",
        base_ref="main", base_sha="a", head_ref="feat", head_sha="b",
        state="open",
        hunks=[Hunk(
            file_path="src/foo.py", old_file_path="src/foo.py",
            old_start=1, old_count=1, new_start=1, new_count=2,
            content="@@ -1 +1,2 @@\n line\n+added\n",
        )],
    )


def _batch(**kw) -> ReviewBatch:
    """A finished batch, the way the orchestrator finishes one."""
    b = ReviewBatch(pull_request=_pr(), **kw)
    b.verdict = b.compute_verdict()
    b.apply_partial_banner()
    b.mark_complete()
    return b


@dataclass
class _Result:
    """What the orchestrator hands the recorder."""

    batch: ReviewBatch
    posted: bool = True


@pytest.fixture
def store(tmp_path: Path) -> ReviewRunStore:
    return ReviewRunStore(tmp_path / "review_runs.db")


def _legacy_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO review_runs (id, user_id, pr_ref, status, verdict,"
        " summary, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("old", "u1", "github:acme/api#1", "complete", "approve",
         "Looks fine.", "2020-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    return path


# ─── the batch knows, in one place ───────────────────────────────────


def test_a_failed_critical_agent_makes_the_run_partial():
    b = _batch(agents_run=["architect", "quality"], agents_failed=["security"])
    assert b.run_status is ReviewRunStatus.PARTIAL
    assert b.verdict is not ReviewVerdict.APPROVE


def test_a_clean_run_is_complete_and_carries_no_banner():
    b = _batch(agents_run=["architect", "security", "quality"])
    assert b.run_status is ReviewRunStatus.COMPLETE
    assert b.partial_banner == ""
    assert "PARTIAL" not in b.summary
    assert b.verdict is ReviewVerdict.APPROVE


def test_a_non_critical_gap_is_still_a_gap():
    """Kodus's distinction: a partial-only failure posts every comment it has
    and adds a short notice. It does not block the approval, and it does not
    pretend the review looked everywhere."""
    b = _batch(agents_run=["architect", "security"], agents_failed=["quality"])
    assert b.run_status is ReviewRunStatus.PARTIAL
    assert b.verdict is ReviewVerdict.APPROVE
    assert "quality" in b.summary
    assert "cannot be an approval" not in b.summary, (
        "a quality agent's absence never touched the verdict — saying it did "
        "is the same kind of untrue claim as the old "
        '"downgraded from APPROVE to COMMENT" was'
    )


def test_the_banner_names_every_agent_the_row_will_name():
    """The two used to be built independently: the banner from a literal
    `{"architect", "security"}` re-spelled in the orchestrator, the verdict
    from `_CRITICAL_AGENTS`. Nothing kept them equal."""
    b = _batch(agents_run=["quality"], agents_failed=["security", "architect"])
    for agent in b.agents_failed:
        assert agent in b.partial_banner
    assert b.failed_critical_agents == ["security", "architect"]


def test_the_banner_is_not_doubled_if_applied_twice():
    # `agents_run` is not decoration here. Left out, this batch says every
    # agent it dispatched failed, which is a FAILED run and a different banner
    # — see test_a_review_that_answered_nothing_is_not_partial. The subject of
    # this test is the partial banner, so the batch has to be a partial one.
    b = ReviewBatch(pull_request=_pr(), agents_run=["architect", "quality"],
                    agents_failed=["security"], summary="Original body.")
    b.apply_partial_banner()
    b.apply_partial_banner()
    assert b.summary.count("PARTIAL REVIEW") == 1
    assert b.summary.endswith("Original body.")


# ─── it survives the run ─────────────────────────────────────────────


def test_a_partial_review_reads_back_naming_the_agent(store: ReviewRunStore):
    """The gap this wave exists to close: after the run, ask the store which
    agent failed, and be told."""
    from src.api.review_runs import record_completed_review

    store.insert(ReviewRun(id="r1", user_id="u1", pr_ref="github:acme/api#7"))
    batch = _batch(agents_run=["architect", "quality", "tests"],
                   agents_failed=["security"])
    record_completed_review(_Result(batch=batch), run_id="r1", store=store)

    row = store.get("r1")
    assert row is not None
    assert row.status == ReviewRunStatus.PARTIAL.value
    assert row.agents_failed == ["security"]
    assert row.agents_run == ["architect", "quality", "tests"]


def test_a_clean_review_reads_back_complete(store: ReviewRunStore):
    from src.api.review_runs import record_completed_review

    store.insert(ReviewRun(id="r2", user_id="u1", pr_ref="github:acme/api#8"))
    batch = _batch(agents_run=["architect", "security"])
    record_completed_review(_Result(batch=batch), run_id="r2", store=store)

    row = store.get("r2")
    assert row is not None
    assert row.status == ReviewRunStatus.COMPLETE.value
    assert row.agents_failed == [], (
        "an empty list is the pipeline saying it tracked this and nothing "
        "failed — it must not come back as None, which means nobody looked"
    )


def test_the_banner_and_the_columns_agree(store: ReviewRunStore):
    """One source of truth, checked from the outside: whatever the row says
    failed is what the prose in the same row says failed."""
    from src.api.review_runs import record_completed_review

    store.insert(ReviewRun(id="r3", user_id="u1", pr_ref="github:acme/api#9"))
    batch = _batch(agents_run=["quality"], agents_failed=["architect", "security"])
    record_completed_review(_Result(batch=batch), run_id="r3", store=store)

    row = store.get("r3")
    assert row is not None
    assert row.agents_failed
    for agent in row.agents_failed:
        assert agent in row.summary
    assert (row.status == ReviewRunStatus.PARTIAL.value) is ("PARTIAL REVIEW" in row.summary)


def test_a_finding_still_drives_the_verdict_of_a_partial_run(store: ReviewRunStore):
    """Partial is about coverage, not severity. A critical finding from an
    agent that DID answer must still request changes."""
    from src.api.review_runs import record_completed_review

    store.insert(ReviewRun(id="r4", user_id="u1", pr_ref="github:acme/api#10"))
    finding = Finding(
        file_path="src/foo.py", line=2, severity=FindingSeverity.CRITICAL,
        title="SQL injection", body="concatenated query", agent="architect",
    )
    batch = _batch(findings=[finding], agents_run=["architect"],
                   agents_failed=["security"])
    record_completed_review(_Result(batch=batch), run_id="r4", store=store)

    row = store.get("r4")
    assert row is not None
    assert row.verdict == ReviewVerdict.REQUEST_CHANGES.value
    assert row.status == ReviewRunStatus.PARTIAL.value


# ─── the other completion writer ─────────────────────────────────────


def test_the_ui_path_writes_the_same_status(monkeypatch, store: ReviewRunStore):
    """Two writers finish a review — the background task behind
    /api/reviews/trigger, and record_completed_review for webhook/poller runs.
    They had already drifted once (one grew `evidence_kind` and
    `cross_repo_callers`, the other had nothing), so the status is asserted on
    both rather than on whichever one was easier to reach."""
    import src.api.routers.reviews as reviews_mod
    import src.review.orchestrator as orch_mod
    import src.review.providers as providers_mod

    batch = _batch(agents_run=["architect", "tests"], agents_failed=["security"])

    class _Provider:
        def close(self) -> None:
            pass

    class _Orchestrator:
        def review(self, *a, **kw):
            return _Result(batch=batch)

    monkeypatch.setattr(orch_mod, "ReviewOrchestrator", _Orchestrator)
    monkeypatch.setattr(providers_mod, "get_provider_for",
                        lambda *a, **kw: _Provider())
    monkeypatch.setattr(reviews_mod, "get_review_run_store", lambda: store)

    store.insert(ReviewRun(id="r5", user_id="u1", pr_ref="github:acme/api#11"))
    reviews_mod._run_review_task(
        pr_ref="github:acme/api#11", post_comments=False,
        run_id="r5", user_id="u1", workspace_id="ws1",
    )

    row = store.get("r5")
    assert row is not None, "the background task did not finish the row"
    assert row.status == ReviewRunStatus.PARTIAL.value
    assert row.agents_failed == ["security"]


# ─── the migration ───────────────────────────────────────────────────


def _columns(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(review_runs)")}
    finally:
        conn.close()


def test_a_fresh_database_has_the_columns(tmp_path: Path):
    ReviewRunStore(tmp_path / "fresh.db")
    assert {"agents_run", "agents_failed"} <= _columns(tmp_path / "fresh.db")


def test_the_migration_applies_to_a_database_that_predates_it(tmp_path: Path):
    db = _legacy_db(tmp_path / "legacy.db")
    assert "agents_failed" not in _columns(db)
    ReviewRunStore(db)
    assert {"agents_run", "agents_failed"} <= _columns(db)


def test_opening_it_again_is_not_an_error(tmp_path: Path):
    """The ALTERs run on every open. A second run answers "duplicate column
    name", and an unhandled one here takes the whole API process down at
    import — which is the shape of the outage the alembic chain test was
    written for."""
    db = _legacy_db(tmp_path / "legacy.db")
    for _ in range(3):
        ReviewRunStore(db)
    assert {"agents_run", "agents_failed"} <= _columns(db)


def test_a_row_written_before_the_columns_is_unknown_not_clean(tmp_path: Path):
    """The point of nullable-with-no-backfill. This run may well have lost its
    security agent; nobody recorded it, and saying `[]` would be the product
    inventing an all-clear for a review it cannot account for."""
    db = _legacy_db(tmp_path / "legacy.db")
    row = ReviewRunStore(db).get("old")
    assert row is not None
    assert row.agents_run is None
    assert row.agents_failed is None
    assert row.agents_failed != [], "unknown collapsed into 'nothing failed'"


def test_reading_survives_the_columns_going_away_again(tmp_path: Path):
    """The reverse direction. These are additive ALTERs with no down step, so
    "reverses" means: put the code in front of a table that does not have the
    columns — a rollback, or a replica that has not caught up — and it reads
    unknown instead of raising, exactly like the drift column before it."""
    from src.api.review_runs import _agent_roster

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE r (id TEXT)")
    conn.execute("INSERT INTO r VALUES ('x')")
    row = conn.execute("SELECT * FROM r").fetchone()
    assert _agent_roster(row, "agents_run") is None
    assert _agent_roster(row, "agents_failed") is None


@pytest.mark.parametrize("stored,expected", [
    (json.dumps(["security"]), ["security"]),
    (json.dumps([]), []),
    (None, None),
    ("", None),
    ("not json at all", None),
    (json.dumps({"failed": ["security"]}), None),
    (json.dumps(["security", 7]), ["security", "7"]),
])
def test_reading_a_roster_never_fails_a_history_request(stored, expected):
    """TEXT written by whichever version of the pipeline was deployed at the
    time. Anything unreadable is unknown, and unknown is None."""
    from src.api.review_runs import _agent_roster

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE r (agents_failed TEXT)")
    conn.execute("INSERT INTO r VALUES (?)", (stored,))
    row = conn.execute("SELECT * FROM r").fetchone()
    assert _agent_roster(row, "agents_failed") == expected


# ─── it reaches an API consumer ──────────────────────────────────────


def test_the_api_sends_the_gap():
    from src.api.routers.reviews import _run_to_out

    out = _run_to_out(ReviewRun(
        id="r6", user_id="u1", pr_ref="github:acme/api#12",
        status="partial", verdict="comment",
        agents_run=["architect"], agents_failed=["security"],
    ))
    assert out.status == "partial"
    assert out.agents_failed == ["security"]
    assert out.agents_run == ["architect"]


def test_the_api_does_not_invent_an_all_clear_for_an_old_run():
    from src.api.routers.reviews import _run_to_out

    out = _run_to_out(ReviewRun(
        id="r7", user_id="u1", pr_ref="github:acme/api#13",
        status="complete", verdict="approve",
    ))
    assert out.agents_failed is None
    assert out.agents_run is None


def test_partial_does_not_hijack_the_displayed_verdict():
    """`verdict` in the response doubles as the display state for queued /
    running / failed rows. A partial run posted real comments and reached a
    real verdict, so that is what it shows; the gap travels in its own
    fields. Pinned because folding 'partial' in here would change the UI."""
    from src.api.routers.reviews import _run_to_out

    out = _run_to_out(ReviewRun(
        id="r8", user_id="u1", pr_ref="github:acme/api#14",
        status="partial", verdict="changes", agents_failed=["security"],
    ))
    assert out.verdict == "changes"
