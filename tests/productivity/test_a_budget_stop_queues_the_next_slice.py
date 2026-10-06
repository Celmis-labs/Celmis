"""The queue handlers: a slice that stops early queues its own continuation; errors retry; cancel cancels."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.productivity import sync as ps
from src.sync import handlers
from src.sync import queue as jq

JOB = {"id": "job-1", "attempts": 1, "payload": {
    "workspace_id": "ws-1", "provider": "bitbucket", "repo": "acme/app", "user_id": "u1", "repo_slug": "acme-app"}}


@pytest.fixture
def seen(monkeypatch):
    calls = {"run": [], "enqueue": []}

    def fake_enqueue(*a, **kw):
        calls["enqueue"].append((a, kw))

    monkeypatch.setattr(ps, "enqueue_sync", fake_enqueue)
    calls["monkeypatch"] = monkeypatch
    return calls


def _runs(seen, result):
    def run(*a, **kw):
        seen["run"].append((a, kw))
        return result

    seen["monkeypatch"].setattr(ps, "run_repo_sync", run)


def test_a_budget_stop_queues_a_continuation_with_its_own_key_and_a_pause(seen) -> None:
    _runs(seen, ps.SyncResult("budget", more_work=True))
    asyncio.run(handlers.handle_productivity_sync(JOB))
    ((args, kwargs),) = seen["enqueue"]
    assert args == ("ws-1", "bitbucket", "acme/app")
    assert kwargs["continuation"] == "job-1" and kwargs["delay_seconds"] >= 30
    assert kwargs["user_id"] == "u1" and kwargs["repo_slug"] == "acme-app"


def test_a_rate_limited_slice_comes_back_when_the_provider_allows(seen) -> None:
    until = datetime.now(UTC) + timedelta(minutes=20)
    _runs(seen, ps.SyncResult("rate_limited", more_work=True, resume_at=until))
    asyncio.run(handlers.handle_productivity_sync(JOB))
    delay = seen["enqueue"][0][1]["delay_seconds"]
    assert 19 * 60 < delay <= 20 * 60


def test_a_finished_slice_queues_nothing(seen) -> None:
    _runs(seen, ps.SyncResult("ok"))
    asyncio.run(handlers.handle_productivity_sync(JOB))
    assert seen["enqueue"] == []


def test_a_disabled_or_busy_slice_queues_nothing_and_does_not_fail(seen) -> None:
    for status in ("disabled", "busy"):
        _runs(seen, ps.SyncResult(status))
        asyncio.run(handlers.handle_productivity_sync(JOB))
    assert seen["enqueue"] == []


def test_an_error_fails_the_job_so_the_queue_retries_with_backoff(seen) -> None:
    _runs(seen, ps.SyncResult("error", error="ProviderError: HTTP 500"))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        asyncio.run(handlers.handle_productivity_sync(JOB))


def test_a_cancelled_slice_is_cancelled_not_failed(seen) -> None:
    _runs(seen, ps.SyncResult("cancelled"))
    with pytest.raises(jq.JobCancelled):
        asyncio.run(handlers.handle_productivity_sync(JOB))


def test_a_retry_of_a_full_resync_does_not_reset_the_cursor_again(seen) -> None:
    _runs(seen, ps.SyncResult("ok"))
    first = {**JOB, "payload": {**JOB["payload"], "full": True}, "attempts": 1}
    retry = {**first, "attempts": 2}
    asyncio.run(handlers.handle_productivity_sync(first))
    asyncio.run(handlers.handle_productivity_sync(retry))
    assert [kw["full"] for _, kw in seen["run"]] == [True, False]


def test_the_time_budget_comes_from_the_environment_and_survives_a_typo(seen, monkeypatch) -> None:
    _runs(seen, ps.SyncResult("ok"))
    monkeypatch.setenv("CELMIS_PRODUCTIVITY_JOB_BUDGET_SECONDS", "120")
    asyncio.run(handlers.handle_productivity_sync(JOB))
    monkeypatch.setenv("CELMIS_PRODUCTIVITY_JOB_BUDGET_SECONDS", "ten minutes")
    asyncio.run(handlers.handle_productivity_sync(JOB))
    assert [kw["time_budget"] for _, kw in seen["run"]] == [120.0, ps.DEFAULT_TIME_BUDGET]


def test_the_single_pr_job_asks_again_later_when_rate_limited(seen) -> None:
    until = datetime.now(UTC) + timedelta(minutes=5)
    seen["monkeypatch"].setattr(ps, "refresh_pull_request",
                                lambda *a, **kw: ps.SyncResult("rate_limited", more_work=True, resume_at=until))
    queued = []
    seen["monkeypatch"].setattr(jq, "enqueue", lambda **kw: queued.append(kw))
    job = {"id": "j", "attempts": 1, "payload": {**JOB["payload"], "number": 7}}
    asyncio.run(handlers.handle_productivity_pr(job))
    assert queued[0]["kind"] == jq.KIND_PRODUCTIVITY_PR and queued[0]["dedup_key"].endswith(":7:j")
    assert 4 * 60 < queued[0]["delay_seconds"] <= 5 * 60

def test_the_single_pr_job_comes_back_in_a_minute_when_a_sync_holds_the_repository(seen) -> None:
    seen["monkeypatch"].setattr(ps, "refresh_pull_request",
                                lambda *a, **kw: ps.SyncResult("busy", more_work=True))
    queued = []
    seen["monkeypatch"].setattr(jq, "enqueue", lambda **kw: queued.append(kw))
    job = {"id": "j", "attempts": 1, "payload": {**JOB["payload"], "number": 7}}
    asyncio.run(handlers.handle_productivity_pr(job))
    assert queued[0]["kind"] == jq.KIND_PRODUCTIVITY_PR and queued[0]["delay_seconds"] == 60
    assert queued[0]["dedup_key"].endswith(":7:j")


def test_both_job_kinds_have_a_handler_registered_by_the_worker() -> None:
    import inspect

    from src.sync import worker

    source = inspect.getsource(worker.start_worker)
    assert "KIND_PRODUCTIVITY_SYNC" in source and "KIND_PRODUCTIVITY_PR" in source
