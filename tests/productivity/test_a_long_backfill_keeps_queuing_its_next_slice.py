"""The queue refuses a job whose dedup key is pending OR running, so a chain of slices must not share one key."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from src.productivity import sync as ps
from src.sync import handlers
from src.sync import queue as jq

PAYLOAD = {"workspace_id": "ws-1", "provider": "bitbucket", "repo": "acme/app", "user_id": "u1"}


class DedupQueue:
    """The real queue's rule: a key held by a pending or running row is refused."""

    def __init__(self) -> None:  # noqa: D107
        self.rows: list[dict] = []
        self._ids = itertools.count(1)

    def enqueue(self, kind, payload, dedup_key=None, delay_seconds=0, workspace_id=None, **_):  # noqa: D102
        if dedup_key and any(r["dedup_key"] == dedup_key and r["status"] in ("pending", "running")
                             for r in self.rows):
            return None
        row = {"id": f"job-{next(self._ids)}", "kind": kind, "payload": payload,
               "dedup_key": dedup_key, "status": "pending"}
        self.rows.append(row)
        return row["id"]

    def next_pending(self) -> dict | None:  # noqa: D102
        return next((r for r in self.rows if r["status"] == "pending"), None)


@pytest.fixture
def queue(monkeypatch):
    q = DedupQueue()
    monkeypatch.setattr(jq, "enqueue", q.enqueue)
    return q


def _drive(queue, handler, slices: int) -> int:
    done = 0
    while (job := queue.next_pending()) and done < slices:
        job["status"] = "running"
        asyncio.run(handler({"id": job["id"], "attempts": 1, "payload": job["payload"]}))
        job["status"] = "completed"
        done += 1
    return done


def test_a_backfill_that_stops_on_its_budget_five_times_runs_all_five_slices(queue, monkeypatch) -> None:
    monkeypatch.setattr(ps, "run_repo_sync", lambda *a, **kw: ps.SyncResult("budget", more_work=True))
    ps.enqueue_sync("ws-1", "bitbucket", "acme/app", user_id="u1")
    assert _drive(queue, handlers.handle_productivity_sync, slices=5) == 5
    assert queue.next_pending() is not None          # and the sixth is already waiting


def test_a_slice_that_finishes_ends_the_chain(queue, monkeypatch) -> None:
    results = iter([ps.SyncResult("budget", more_work=True)] * 3 + [ps.SyncResult("ok")])
    monkeypatch.setattr(ps, "run_repo_sync", lambda *a, **kw: next(results))
    ps.enqueue_sync("ws-1", "bitbucket", "acme/app", user_id="u1")
    assert _drive(queue, handlers.handle_productivity_sync, slices=10) == 4
    assert queue.next_pending() is None


def test_a_retried_slice_does_not_queue_its_continuation_twice(queue, monkeypatch) -> None:
    monkeypatch.setattr(ps, "run_repo_sync", lambda *a, **kw: ps.SyncResult("budget", more_work=True))
    job = {"id": "job-1", "attempts": 2, "payload": PAYLOAD}
    asyncio.run(handlers.handle_productivity_sync(job))
    asyncio.run(handlers.handle_productivity_sync(job))
    assert len(queue.rows) == 1


def test_a_pr_refresh_that_is_rate_limited_twice_is_queued_both_times(queue, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    until = datetime.now(UTC) + timedelta(minutes=5)
    monkeypatch.setattr(ps, "refresh_pull_request",
                        lambda *a, **kw: ps.SyncResult("rate_limited", more_work=True, resume_at=until))
    payload = {**PAYLOAD, "number": 7}
    queue.enqueue(jq.KIND_PRODUCTIVITY_PR, payload, dedup_key="prodpr:ws-1:acme/app:7")
    assert _drive(queue, handlers.handle_productivity_pr, slices=4) == 4
