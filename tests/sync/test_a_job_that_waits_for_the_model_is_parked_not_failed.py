"""A job that cannot run because the model is down waits; it does not fail.

The queue only knew two ways out of a handler: return (completed) or raise
(retry with backoff, five times, then dead). A review that found the model
dead did neither well — it returned, so the job completed and nothing ran it
again. Now a handler raises `JobDeferred` and the row is parked as
'deferred':

  * the attempt `dequeue_one` counted is handed back — an outage is not the
    job's fault and must not move it towards 'dead';
  * the dedup index covers 'deferred', so a new event for the same pull
    request folds into the waiting job;
  * the reason, the first deferral and the next check are on the row, and
    survive a restart (it is a table);
  * a manual retry stops the waiting at once;
  * any other exception keeps the old backoff.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

import src.sync.queue as jq
import src.sync.worker as worker
from tests.sync.deferral_support import as_dt, insert, make_engine, row, use


@pytest.fixture
def engine(tmp_path, monkeypatch):
    e = make_engine(tmp_path / "jobs.db")
    use(monkeypatch, e)
    return e


def _own(engine, jid=None):
    """The row is leased to THIS worker, as `dequeue_one` leaves it."""
    with engine.begin() as c:
        c.exec_driver_sql(
            "UPDATE sync_jobs SET locked_by = ? WHERE (? IS NULL OR id = ?)",
            (jq._worker_id(), jid, jid))


def _job(engine, **kw):
    """A job as the worker holds it right after `dequeue_one`: running, one attempt."""
    jid = insert(engine, status="running", attempts=1, deferred_ago=None, **kw)
    _own(engine, jid)
    return {"id": jid, "kind": "review", "attempts": 1, "max_attempts": 5,
            "payload": {}}


def _run(job, handler):
    worker.register("review", handler)
    asyncio.run(worker._process(job))


def test_a_deferral_hands_the_attempt_back_and_parks_the_row(engine):
    job = _job(engine)

    async def _handler(_job):
        raise jq.JobDeferred("the provider is unavailable", code="provider_unavailable")

    _run(job, _handler)
    r = row(engine, job["id"])
    assert r["status"] == "deferred"
    assert r["attempts"] == 0, "an outage is not an attempt"
    assert r["defer_reason"] == "the provider is unavailable"
    assert r["defer_code"] == "provider_unavailable"
    assert r["deferred_since"] is not None and r["finished_at"] is None
    assert r["locked_by"] is None
    assert 50 <= (as_dt(r["next_run_at"]) - datetime.now(UTC)).total_seconds() <= 61


def test_a_deferral_never_walks_a_job_to_dead(engine):
    job = _job(engine)
    # the job is already on its LAST attempt when the model goes down
    with engine.begin() as c:
        c.exec_driver_sql("UPDATE sync_jobs SET attempts = 5")
    job["attempts"] = 5

    async def _handler(_job):
        raise jq.JobDeferred("down")

    _run(job, _handler)
    r = row(engine, job["id"])
    assert r["status"] == "deferred" and r["attempts"] == 4


def test_any_other_exception_still_backs_off_and_burns_an_attempt(engine):
    job = _job(engine)

    async def _handler(_job):
        raise RuntimeError("boom")

    _run(job, _handler)
    r = row(engine, job["id"])
    assert r["status"] == "pending" and r["last_error"].startswith("RuntimeError")
    assert r["deferred_since"] is None


def test_a_deferral_warns_once_per_job_not_once_per_outage_tick(engine, caplog):
    job = _job(engine)
    caplog.set_level(logging.INFO, logger="src.sync.queue")
    assert jq.mark_deferred(job["id"], "the provider is unavailable") is True
    # released, runs, finds the model down again
    with engine.begin() as c:
        c.exec_driver_sql("UPDATE sync_jobs SET status = 'running' WHERE id = ?",
                          (job["id"],))
    _own(engine, job["id"])
    assert jq.mark_deferred(job["id"], "the provider is unavailable") is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "job_deferred " in warnings[0].getMessage()


def test_a_job_deferred_again_waits_longer_each_time(engine):
    job = _job(engine)
    waits = []
    for _ in range(4):
        jq.mark_deferred(job["id"], "down")
        waits.append(round((as_dt(row(engine, job["id"])["next_run_at"])
                            - datetime.now(UTC)).total_seconds() / 60))
        with engine.begin() as c:
            c.exec_driver_sql("UPDATE sync_jobs SET status = 'running'")
        _own(engine)
    assert waits == [1, 2, 5, 10]


def test_a_new_event_for_a_waiting_pull_request_folds_into_the_waiting_job(engine):
    insert(engine, dedup="review:github:o/r#1")
    folded = jq.enqueue(kind="review", payload={"repo": "o/r"},
                        dedup_key="review:github:o/r#1", workspace_id="ws")
    assert folded is None, "a second job for the same PR would review it twice"


def test_the_migration_dedup_index_covers_the_waiting_state():
    import importlib.util
    from pathlib import Path

    path = (Path(__file__).resolve().parents[2] / "alembic" / "versions"
            / "c8d3a5f1b762_sync_jobs_deferred.py")
    spec = importlib.util.spec_from_file_location("m_c8d3a5f1b762", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "'deferred'" in mod._NEW_ACTIVE and "'deferred'" not in mod._OLD_ACTIVE
    assert "'pending','running'" in mod._NEW_ACTIVE


def test_a_manual_retry_stops_the_waiting_and_keeps_the_attempts(engine):
    jid = insert(engine, attempts=2, next_in=timedelta(minutes=10), checks=3)
    assert jq.retry_dead(jid) is True
    r = row(engine, jid)
    assert r["status"] == "pending" and r["attempts"] == 2
    assert as_dt(r["next_run_at"]) <= datetime.now(UTC)


def test_a_dead_job_retried_starts_over_with_a_clean_deferral(engine):
    jid = insert(engine, status="dead", attempts=5, checks=4)
    assert jq.retry_dead(jid) is True
    r = row(engine, jid)
    assert (r["status"], r["attempts"], r["defer_checks"], r["deferred_since"]) == (
        "pending", 0, 0, None)


def test_retry_refuses_a_job_that_is_running_or_done(engine):
    for status in ("running", "completed", "pending"):
        assert jq.retry_dead(insert(engine, status=status, deferred_ago=None)) is False


def test_the_jobs_api_lists_counts_and_retries_the_waiting_state(engine):
    from types import SimpleNamespace

    from src.api.routers import jobs as jobs_api

    jid = insert(engine, ws="ws")
    insert(engine, ws="other")
    admin = SimpleNamespace(is_admin=False, email="a@example.com")

    listed = jobs_api.list_jobs(status="deferred", kind=None, limit=50,
                                user=admin, workspace_id="ws")
    assert [j["id"] for j in listed] == [jid]
    j = listed[0]
    assert j["status"] == "deferred" and j["defer_reason"] == "the provider is unavailable"
    assert isinstance(j["deferred_since"], str) and isinstance(j["next_run_at"], str)
    assert jobs_api.stats(user=admin, workspace_id="ws") == {"deferred": 1}

    assert jobs_api.retry(jid, user=admin, workspace_id="ws") == {"ok": True}
    assert row(engine, jid)["status"] == "pending"
    other = insert(engine, ws="other")
    with pytest.raises(Exception) as caught:  # another tenant's job is a 404
        jobs_api.retry(other, user=admin, workspace_id="ws")
    assert getattr(caught.value, "status_code", None) == 404


def test_the_run_list_finds_the_job_a_deferred_run_waits_on(engine):
    jid = insert(engine, payload={"run_id": "run-1", "provider": "github", "repo": "o/r",
                                  "pr_number": 1, "workspace_id": "ws"})
    insert(engine, status="pending", deferred_ago=None,
           payload={"run_id": "run-2"})
    found = jq.deferred_for_runs(["run-1", "run-2", "run-3"])
    assert set(found) == {"run-1"} and found["run-1"]["id"] == jid
    assert found["run-1"]["defer_reason"] == "the provider is unavailable"
    assert jq.deferred_for_runs([]) == {}
