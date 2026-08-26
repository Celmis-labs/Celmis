"""The run history must carry what the run actually did — not just its verdict.

The providers count what their comment cleanup did ({deleted, failed,
kept_threaded, complete}) precisely so a half-done cleanup cannot pass for a
finished one. That count used to die in `provider_response`, which nothing
persisted: both posting paths (the UI trigger's background task and the
webhook/poller's `record_completed_review`) dropped it on the floor, so the
reviews page could not tell "cleaned" from "left last run's comments on the
PR" — the exact distinction the providers went to some length to report.

These tests pin the whole surface the page reads: cleanup travels from a
posted review into the run row and out through /api/reviews/history; a run
that never cleaned reports None (absence of a report, not a clean one); and a
skipped run still carries its WHY in `summary`, because "skipped" alone is a
verdict where the user needed an explanation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user
from src.api.review_runs import (
    ReviewRun,
    ReviewRunStore,
    record_completed_review,
)
from src.api.routers import reviews as reviews_router
from src.review.models import ReviewRunStatus, ReviewVerdict
from src.users import User

WS = "ws-1"
CLEANUP = {"deleted": 3, "failed": 1, "kept_threaded": 2, "complete": False}


@pytest.fixture
def store(tmp_path, monkeypatch) -> ReviewRunStore:
    s = ReviewRunStore(tmp_path / "runs.db")
    # get_review_run_store() reads this module global on every call, so both
    # the router handlers and the background task see the temp store.
    monkeypatch.setattr("src.api.review_runs._default_store", s)
    return s


@pytest.fixture
def client(store) -> TestClient:
    app = FastAPI()
    app.include_router(reviews_router.router)
    app.dependency_overrides[get_current_user] = lambda: User(
        id="u-1", email="reviewer@example.com")
    app.dependency_overrides[current_workspace_id] = lambda: WS
    return TestClient(app)


def _insert(store: ReviewRunStore, run_id: str, **updates) -> None:
    store.insert(ReviewRun(id=run_id, user_id="u-1",
                           pr_ref="github:o/r#7", workspace_id=WS))
    if updates:
        store.update(run_id, **updates)


def _history_row(client: TestClient, run_id: str) -> dict:
    resp = client.get("/api/reviews/history")
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json() if r["id"] == run_id]
    assert rows, "run missing from its own workspace's history"
    return rows[0]


# ─── the serialisation the page reads ────────────────────────────────


def test_history_carries_the_cleanup_outcome(client, store):
    """deleted/failed/kept_threaded/complete reach the UI verbatim."""
    import json

    _insert(store, "r-posted", status="partial", verdict="comment",
            agents_run=["architect"], agents_failed=["security"],
            cleanup_json=json.dumps(CLEANUP), finished=True)

    row = _history_row(client, "r-posted")
    assert row["cleanup"] == CLEANUP
    assert row["status"] == "partial"
    # The gap travels beside the verdict, not folded into it — a partial run
    # still has a real verdict, and the page names WHO failed.
    assert row["verdict"] == "comment"
    assert row["agents_failed"] == ["security"]


def test_a_run_that_never_cleaned_reports_no_cleanup(client, store):
    """None, not {} — absence of a report must not read as a clean cleanup."""
    _insert(store, "r-dry", status="complete", verdict="approve",
            finished=True)

    assert _history_row(client, "r-dry")["cleanup"] is None


def test_a_skipped_run_still_says_why(client, store):
    """'skipped' is an explanation, and the banner text is the explanation."""
    banner = "PR is draft — review skipped."
    _insert(store, "r-skip", status="skipped", verdict="skipped",
            summary=banner, finished=True)

    row = _history_row(client, "r-skip")
    assert row["status"] == "skipped"
    assert row["summary"] == banner


# ─── the two posting paths that must persist it ──────────────────────


def _batch(status: ReviewRunStatus = ReviewRunStatus.COMPLETE):
    return SimpleNamespace(
        run_status=status, agents_run=["architect"], agents_failed=[],
        verdict=ReviewVerdict.APPROVE, findings=[],
        critical_count=0, error_count=0, warning_count=0, info_count=0,
        cross_repo_callers=0, elapsed_seconds=1.5, summary="ok",
        cost_usd=None, cost_source=None, tokens_in=0, tokens_out=0,
    )


@pytest.mark.parametrize("provider_response,expected", [
    ({"cleanup": CLEANUP}, CLEANUP),
    # A dry-run's {} and a failed post's {"error": ...} carry no report —
    # inventing an empty one would turn "never cleaned" into "cleaned".
    ({}, None),
    ({"error": "GitHub review POST failed 502"}, None),
])
def test_the_webhook_path_persists_what_the_cleanup_did(
        store, provider_response, expected):
    _insert(store, "r-hook", status="running")
    result = SimpleNamespace(batch=_batch(), posted=True,
                             provider_response=provider_response)

    record_completed_review(result, run_id="r-hook", store=store)

    assert store.get("r-hook").cleanup == expected


def test_the_ui_trigger_path_persists_what_the_cleanup_did(
        client, store, monkeypatch):
    """The background task the trigger queues writes the same report."""
    _insert(store, "r-ui", status="queued")

    pr = SimpleNamespace(head_sha="abc123", head_ref="feature", raw_diff="",
                         provider="github", repo="o/r", number=7)

    class FakeOrchestrator:
        def review(self, *args, **kwargs):
            batch = _batch(ReviewRunStatus.PARTIAL)
            batch.agents_failed = ["security"]
            batch.verdict = ReviewVerdict.COMMENT
            batch.pull_request = pr
            return SimpleNamespace(batch=batch, posted=True,
                                   provider_response={"cleanup": CLEANUP})

    provider = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr("src.cli._parse_pr_ref",
                        lambda ref: ("github", "o/r", 7))
    monkeypatch.setattr("src.review.providers.get_provider_for",
                        lambda *a, **k: provider)
    monkeypatch.setattr("src.review.orchestrator.ReviewOrchestrator",
                        FakeOrchestrator)

    reviews_router._run_review_task(
        pr_ref="github:o/r#7", post_comments=True,
        run_id="r-ui", user_id="u-1", workspace_id=WS,
    )

    # End to end: persisted by the task AND served by the run endpoint the
    # page polls, so a break in either half fails here.
    row = client.get("/api/reviews/r-ui").json()
    assert row["cleanup"] == CLEANUP
    assert row["status"] == "partial"
    assert row["agents_failed"] == ["security"]
