"""The reviews API serves what the run changed behind the operator's back.

GET /api/reviews/{id} and /history exposed none of the runtime's self-heals —
not the clamped ceiling, not the dropped reasoning level, not the dropped
temperature, not the fallback model — so the reviews page could not render
them and the operator never learned which knob to turn. Pinned here: the
detail view carries the full `parameter_adjustments` list and a count; the
history list carries the count WITHOUT the list, so it can badge fifty rows
without shipping every agent's adjustments; a row written before the record
is unknown (null), not clean ([]); and both completion writers put the same
dicts where the endpoints read them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user
from src.api.review_runs import ReviewRun, ReviewRunStore, record_completed_review
from src.api.routers import reviews as reviews_router
from src.llm.capabilities import ParameterAdjustment
from src.review.models import ReviewRunStatus, ReviewVerdict
from src.users import User

WS = "ws-1"

CLAMP = {
    "agent": "architect", "parameter": "max_output_tokens", "requested": 100000,
    "sent": 65535, "action": "clamped", "reason": "model ceiling is 65535",
    "model": "gemini/gemini-3.7-flash",
}
REASONING = {
    "agent": "security", "parameter": "reasoning", "requested": "minimal",
    "sent": None, "action": "dropped",
    "reason": "Thinking level MINIMAL is not supported for this model.",
    "model": "gemini/gemini-3.7-flash",
}
TEMPERATURE = {
    "agent": "architect", "parameter": "temperature", "requested": 0.1,
    "sent": None, "action": "dropped",
    "reason": "temperature: only temperature=1 is supported for this model.",
    "model": "anthropic/claude-sonnet-5",
}
SWAP = {
    "agent": "quality", "parameter": "model", "requested": "gemini/gemini-3.7-flash",
    "sent": "gemini/gemini-3.6-flash", "action": "swapped",
    "reason": "provider quota exhausted", "model": "gemini/gemini-3.7-flash",
}
ALL = [CLAMP, REASONING, TEMPERATURE, SWAP]


@pytest.fixture
def store(tmp_path, monkeypatch) -> ReviewRunStore:
    s = ReviewRunStore(tmp_path / "runs.db")
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
    store.insert(ReviewRun(id=run_id, user_id="u-1", pr_ref="github:o/r#7", workspace_id=WS))
    if updates:
        store.update(run_id, **updates)


def _history_row(client: TestClient, run_id: str) -> dict:
    resp = client.get("/api/reviews/history")
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json() if r["id"] == run_id]
    assert rows, "run missing from its own workspace's history"
    return rows[0]


def _detail(client: TestClient, run_id: str) -> dict:
    resp = client.get(f"/api/reviews/{run_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ─── the two shapes ──────────────────────────────────────────────────


def test_the_detail_view_carries_the_list_and_the_count(client, store):
    _insert(store, "r-adj", status="complete", verdict="approve",
            agents_run=["architect", "security", "quality"],
            parameter_adjustments=ALL, finished=True)

    row = _detail(client, "r-adj")
    assert row["parameter_adjustments"] == ALL
    assert row["adjustments_count"] == 4
    # Every kind in the contract's vocabulary, verbatim.
    assert {a["parameter"] for a in row["parameter_adjustments"]} == {
        "max_output_tokens", "reasoning", "temperature", "model"}
    assert {a["action"] for a in row["parameter_adjustments"]} == {
        "clamped", "dropped", "swapped"}


def test_the_history_carries_the_count_without_the_list(client, store):
    _insert(store, "r-adj", status="complete", verdict="approve",
            agents_run=["architect"], parameter_adjustments=ALL, finished=True)

    row = _history_row(client, "r-adj")
    assert row["adjustments_count"] == 4
    assert row["parameter_adjustments"] is None, (
        "the list view badges; it does not ship every agent's adjustments"
    )


def test_a_run_that_adjusted_nothing_says_so(client, store):
    _insert(store, "r-clean", status="complete", verdict="approve",
            agents_run=["architect"], parameter_adjustments=[], finished=True)

    detail = _detail(client, "r-clean")
    assert detail["parameter_adjustments"] == []
    assert detail["adjustments_count"] == 0
    assert _history_row(client, "r-clean")["adjustments_count"] == 0


def test_a_row_written_before_the_record_is_unknown_not_clean(client, store):
    _insert(store, "r-old", status="complete", verdict="approve", finished=True)

    detail = _detail(client, "r-old")
    assert detail["parameter_adjustments"] is None
    assert detail["adjustments_count"] == 0


def test_a_row_another_version_shaped_does_not_fail_the_request(client, store):
    """Rows are JSON written by whichever pipeline was deployed at the time:
    a grown key or a missing one must not 500 the page."""
    odd = [
        {"parameter": "reasoning", "requested": "minimal", "action": "dropped",
         "reason": "r", "future_key": True},          # grown
        {"agent": "architect"},                       # shrunk
    ]
    _insert(store, "r-odd", status="complete", verdict="approve", finished=True)
    store.update("r-odd", parameter_adjustments=odd)

    detail = _detail(client, "r-odd")
    assert detail["adjustments_count"] == 2
    assert detail["parameter_adjustments"][0]["parameter"] == "reasoning"
    assert detail["parameter_adjustments"][1]["agent"] == "architect"
    assert client.get("/api/reviews/history").status_code == 200


# ─── the two completion writers ──────────────────────────────────────


def _batch(adjustments, status=ReviewRunStatus.COMPLETE):
    return SimpleNamespace(
        run_status=status, agents_run=["architect"], agents_failed=[],
        verdict=ReviewVerdict.APPROVE, findings=[],
        critical_count=0, error_count=0, warning_count=0, info_count=0,
        cross_repo_callers=0, elapsed_seconds=1.5, summary="ok",
        cost_usd=None, cost_source=None, tokens_in=0, tokens_out=0,
        parameter_adjustments=adjustments,
    )


def test_the_webhook_path_persists_the_adjustments(client, store):
    """`record_completed_review` — what a webhook or poller run goes through —
    with the dataclasses the orchestrator actually builds."""
    _insert(store, "r-hook", status="running")
    objects = [ParameterAdjustment(**a) for a in ALL]

    record_completed_review(
        SimpleNamespace(batch=_batch(objects), posted=True, provider_response={}),
        run_id="r-hook", store=store,
    )

    assert _detail(client, "r-hook")["parameter_adjustments"] == ALL


def test_the_ui_trigger_path_persists_the_adjustments(client, store, monkeypatch):
    _insert(store, "r-ui", status="queued")
    pr = SimpleNamespace(head_sha="abc123", head_ref="feature", raw_diff="",
                         provider="github", repo="o/r", number=7)

    class FakeOrchestrator:
        def review(self, *args, **kwargs):
            batch = _batch([ParameterAdjustment(**a) for a in ALL])
            batch.pull_request = pr
            return SimpleNamespace(batch=batch, posted=True, provider_response={})

    monkeypatch.setattr("src.cli._parse_pr_ref", lambda ref: ("github", "o/r", 7))
    monkeypatch.setattr("src.review.providers.get_provider_for",
                        lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("src.review.orchestrator.ReviewOrchestrator", FakeOrchestrator)

    reviews_router._run_review_task(
        pr_ref="github:o/r#7", post_comments=True,
        run_id="r-ui", user_id="u-1", workspace_id=WS,
    )

    detail = _detail(client, "r-ui")
    assert detail["parameter_adjustments"] == ALL
    assert detail["adjustments_count"] == 4
    assert _history_row(client, "r-ui")["adjustments_count"] == 4


def test_a_batch_without_the_field_is_recorded_as_unknown(client, store):
    """An engine that does not report adjustments leaves the column NULL —
    null on the detail view, never [] — so the page cannot claim a review
    sent exactly what was asked when nobody checked."""
    _insert(store, "r-legacy-engine", status="running")
    batch = _batch([])
    del batch.parameter_adjustments

    record_completed_review(
        SimpleNamespace(batch=batch, posted=False, provider_response={}),
        run_id="r-legacy-engine", store=store,
    )

    assert _detail(client, "r-legacy-engine")["parameter_adjustments"] is None


def test_the_stored_json_is_the_contract_shape(store):
    """What sits in the column is the wire dict itself, so a consumer reading
    the database directly sees the same keys the API serves."""
    _insert(store, "r-raw", status="complete", verdict="approve",
            parameter_adjustments=[CLAMP], finished=True)

    import sqlite3
    conn = sqlite3.connect(store.db_path)
    raw = conn.execute("SELECT adjustments_json FROM review_runs WHERE id = 'r-raw'").fetchone()[0]
    conn.close()
    assert json.loads(raw) == [CLAMP]
