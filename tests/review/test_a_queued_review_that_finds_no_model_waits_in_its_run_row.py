"""The run row of a review that waits: deferred, then running again, then done.

Through `execute_review` with the model scripted:

  * preflight says no, the queue can wait: the row is `deferred`, a visible
    `model_wait` stage says why, `ModelUnavailable` reaches the worker, and the
    PR has not been touched;
  * the same with no queue behind it (inline, CLI): the row is `failed` with
    the curated reason, never the exception's text;
  * the model back: the same row runs, the wait stage ends as a success;
  * an outage in the middle of the review (every agent down) parks the row too.
"""

from __future__ import annotations

import pytest

import src.review.model_preflight as mp
from src.review.model_preflight import ModelCheck, ModelUnavailable
from tests.review.test_a_review_says_how_it_got_there import (  # noqa: F401 — fixtures
    POLICY,
    _Agent,
    _orch,
    _Provider,
    _wire,
    no_ledger,
    queue,
    store,
)
from tests.review.test_the_pr_hears_the_review_begin_and_end import _pr, env  # noqa: F401

DOWN = ModelCheck(False, "provider_unavailable", "the model provider is unavailable")


def _queued(monkeypatch, agents=None, pr=None):
    _wire(monkeypatch, _orch(monkeypatch, agents or [_Agent("defect")], policy=POLICY),
          _Provider(pr or _pr()))


def _payload(queue, store):  # noqa: F811
    from src.review.dispatch import enqueue_review_run

    res = enqueue_review_run("github", "o/r", 1, user_id="u", workspace_id="ws",
                             source="manual")
    return res, queue[0]["payload"]


def test_a_review_waits_when_the_model_is_down_and_the_queue_can_wait(
        env, monkeypatch, store, queue, no_ledger):  # noqa: F811
    from src.review.dispatch import execute_review

    _queued(monkeypatch)
    monkeypatch.setattr(mp, "check_for_review", lambda *a, **kw: DOWN)
    res, payload = _payload(queue, store)

    with pytest.raises(ModelUnavailable) as caught:
        execute_review(payload, can_defer=True)

    assert caught.value.reason == "the model provider is unavailable"
    row = store.get(res.run_id)
    assert row.status == "deferred" and not row.finished_at
    wait = next(s for s in row.stages if s["key"] == "model_wait")
    assert wait["status"] == "running"
    assert "Waiting for the model" in row.summary
    assert no_ledger == [], "a wait is not a failed review"
    assert [s["key"] for s in row.stages if s["key"] == "fetch_pr"] == [], (
        "the review never began, so the PR was never fetched")


def test_without_a_queue_the_same_outage_is_a_failed_run_with_a_curated_reason(
        env, monkeypatch, store, queue, no_ledger):  # noqa: F811
    from src.review.dispatch import execute_review

    _queued(monkeypatch)
    monkeypatch.setattr(mp, "check_for_review", lambda *a, **kw: DOWN)
    res, payload = _payload(queue, store)

    with pytest.raises(ModelUnavailable):
        execute_review(payload)  # can_defer defaults to False

    row = store.get(res.run_id)
    assert row.status == "failed" and row.finished_at
    check = next(s for s in row.stages if s["key"] == "model_check")
    assert check["status"] == "failed"
    assert "the model provider is unavailable" in check["reason"]
    assert any("failed" in c for c in no_ledger)


def test_the_model_back_restarts_the_same_run_and_ends_the_wait(
        env, monkeypatch, store, queue, no_ledger):  # noqa: F811
    from src.review.dispatch import execute_review

    _queued(monkeypatch)
    answers = [DOWN, ModelCheck(True)]
    monkeypatch.setattr(mp, "check_for_review", lambda *a, **kw: answers.pop(0))
    res, payload = _payload(queue, store)

    with pytest.raises(ModelUnavailable):
        execute_review(payload, can_defer=True)
    assert store.get(res.run_id).status == "deferred"

    execute_review(payload, can_defer=True)

    row = store.get(res.run_id)
    assert row.status not in ("deferred", "failed", "running") and row.finished_at
    wait = next(s for s in row.stages if s["key"] == "model_wait")
    assert wait["status"] == "success"
    assert not any(s["key"] == "retry" for s in row.stages), (
        "coming back from a wait is not a retry of a failure")


def test_a_second_wait_does_not_stack_a_second_running_stage(
        env, monkeypatch, store, queue, no_ledger):  # noqa: F811
    from src.review.dispatch import execute_review

    _queued(monkeypatch)
    monkeypatch.setattr(mp, "check_for_review", lambda *a, **kw: DOWN)
    res, payload = _payload(queue, store)
    for _ in range(2):
        with pytest.raises(ModelUnavailable):
            execute_review(payload, can_defer=True)

    waits = [s for s in store.get(res.run_id).stages if s["key"] == "model_wait"]
    assert [s["status"] for s in waits].count("running") == 1


def test_an_outage_during_the_review_parks_the_run(
        env, monkeypatch, store, queue, no_ledger):  # noqa: F811
    from src.review.agents.base import AgentRunResult
    from src.review.dispatch import execute_review

    class _Down(_Agent):
        def review(self, context):
            return AgentRunResult(agent=self.name, error="boom", error_code="rate_limited")

    _queued(monkeypatch, agents=[_Down("defect")])
    res, payload = _payload(queue, store)

    with pytest.raises(ModelUnavailable):
        execute_review(payload, can_defer=True)

    row = store.get(res.run_id)
    assert row.status == "deferred" and not row.finished_at
    assert any(s["key"] == "model_wait" and s["status"] == "running" for s in row.stages)
    assert no_ledger == []


def test_a_second_request_for_a_waiting_pr_says_it_waits_for_the_model(
        store, queue, no_ledger, monkeypatch):  # noqa: F811
    import src.sync.queue as q
    from src.review.dispatch import enqueue_review_run

    enqueue_review_run("github", "o/r", 1, user_id="u", workspace_id="ws")
    seen = []
    monkeypatch.setattr(q, "is_deferred", lambda key: seen.append(key) or True)
    second = enqueue_review_run("github", "o/r", 1, user_id="u", workspace_id="ws")

    assert second.status == "duplicate" and seen == ["review:github:o/r#1"]
    row = store.get(second.run_id)
    assert "waiting for the model" in row.status_reason
    assert "Retry now" in row.status_reason
    assert "already queued" not in row.status_reason
