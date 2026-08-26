"""Sync worker — pulls jobs off the Postgres queue and executes handlers.

Runs as a background asyncio task started from FastAPI startup. Scaling:
concurrency = CELMIS_SYNC_WORKER_CONCURRENCY (default 2). For 10
repos + 3 teams peak ~50 jobs/day, 2 workers is plenty; the SKIP LOCKED
dequeue means we can bump concurrency by env var without code change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from src.sync import queue as jq

logger = logging.getLogger(__name__)


_POLL_INTERVAL = float(os.environ.get("CELMIS_SYNC_POLL_SECONDS", "5"))
_CONCURRENCY = int(os.environ.get("CELMIS_SYNC_WORKER_CONCURRENCY", "2"))

_TASK: asyncio.Task | None = None
_HANDLERS: dict[str, Callable[[dict], Awaitable[None]]] = {}


def register(kind: str, handler: Callable[[dict], Awaitable[None]]) -> None:
    """Register an async handler for a job kind. Idempotent."""
    _HANDLERS[kind] = handler
    logger.info("sync_handler_registered kind=%s", kind)


def start_worker() -> None:
    """Kick off the dispatcher. Idempotent."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    # Register built-in handlers on start (importing here breaks any
    # cycle risk if handlers reference queue module during import).
    from src.sync import handlers as h
    register(jq.KIND_REVIEW, h.handle_review)
    register(jq.KIND_INDEX_REPO, h.handle_index_repo)
    register(jq.KIND_INDEX_REPO_FULL, h.handle_index_repo_full)
    register(jq.KIND_OWNERSHIP_REBUILD, h.handle_ownership_rebuild)
    register(jq.KIND_CROSS_REPO_MATERIALIZE, h.handle_cross_repo_materialize)
    register(jq.KIND_REINDEX_QDRANT, h.handle_reindex_qdrant)
    register(jq.KIND_REGENERATE_NOTES, h.handle_regenerate_notes)
    register(jq.KIND_GENERATE_VAULT, h.handle_generate_vault)
    register(jq.KIND_DEPS_AUDIT, h.handle_deps_audit)
    register(jq.KIND_AUTOMATION_PLAN, h.handle_automation_plan)

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _TASK = loop.create_task(_dispatch_forever())
    logger.info("sync_worker_started concurrency=%d poll_s=%.1f",
                _CONCURRENCY, _POLL_INTERVAL)


async def _dispatch_forever() -> None:
    """CONCURRENCY independent worker loops. Each loop pulls its next job as
    soon as it finishes the previous one — a long job (hours-long vault
    generation) occupies exactly one slot instead of barrier-blocking the
    whole queue until it completes."""
    await asyncio.gather(*[_worker_loop(i) for i in range(_CONCURRENCY)])


async def _worker_loop(slot: int) -> None:
    # `locked_by` has to name THIS loop, not the process the loops share —
    # otherwise every lease guard is vacuous between siblings. See
    # `jq._worker_id`.
    jq.set_worker_slot(slot)
    while True:
        try:
            job = await asyncio.to_thread(jq.dequeue_one)
            if job is None:
                await asyncio.sleep(_POLL_INTERVAL)
                continue
            await _process(job)
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync_worker_iter_failed slot=%d err=%s", slot, exc)
            await asyncio.sleep(_POLL_INTERVAL)


async def _process(job: dict[str, Any]) -> None:
    kind = job["kind"]
    handler = _HANDLERS.get(kind)
    if handler is None:
        logger.error("sync_no_handler kind=%s id=%s", kind, job["id"])
        await asyncio.to_thread(
            jq.mark_failure, job["id"],
            f"no handler registered for kind {kind!r}",
            attempts=job["attempts"], max_attempts=job["max_attempts"],
        )
        return
    logger.info("sync_job_start id=%s kind=%s attempt=%d",
                job["id"], kind, job["attempts"])
    heartbeat = asyncio.create_task(_hold_lease(job["id"], kind))
    try:
        await handler(job)
    except jq.JobCancelled as exc:
        logger.info("sync_job_cancelled id=%s kind=%s", job["id"], kind)
        await asyncio.to_thread(
            jq.mark_cancelled, job["id"], str(exc) or "cancelled by user")
        return
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:2000]}"
        logger.warning("sync_job_failed id=%s err=%s", job["id"], err[:300])
        await asyncio.to_thread(
            jq.mark_failure, job["id"], err,
            attempts=job["attempts"], max_attempts=job["max_attempts"],
        )
        return
    finally:
        # Every exit, including the returns above: a heartbeat outliving its
        # job would hold a lease on a row nothing is working on, which is the
        # same lie as the one it exists to stop, pointing the other way.
        heartbeat.cancel()
    await asyncio.to_thread(jq.mark_complete, job["id"])
    logger.info("sync_job_done id=%s kind=%s", job["id"], kind)


async def _hold_lease(job_id: str, kind: str) -> None:
    """Say "still alive" for as long as the handler runs.

    `dequeue_one` stamps `locked_until` once, and `CELMIS_JOB_LEASE_SECONDS`
    defaults to 600. Nothing moved it afterwards, so a handler that took
    longer than ten minutes had its row reclaimed and re-run by a sibling loop
    while it was still working. Measured on this install: 3.5% of 517 real
    reviews ran past 600s (p99 1486s, max 1578s) — about one review in
    twenty-nine paid for twice and posted twice.

    Renewing at a third of the lease means two consecutive renewals can be
    lost — to a database blip, a slow query, a paused container — before
    anything reclaims the job.
    """
    interval = max(5.0, jq.lease_seconds() / 3)
    while True:
        await asyncio.sleep(interval)
        try:
            held = await asyncio.to_thread(jq.renew_lease, job_id)
        except Exception as exc:  # noqa: BLE001
            # A failed renewal is not a failed job. Say so and try again; the
            # lease has two more intervals before it lapses.
            logger.warning("job_lease_renew_failed id=%s err=%s", job_id, exc)
            continue
        if not held:
            # Someone else owns the row now. We cannot un-run what we are
            # doing, but the duplicate must not be silent — it is two reviews,
            # two bills and two comment threads on one pull request.
            logger.error(
                "job_lease_lost id=%s kind=%s — another worker holds this job; "
                "it is running twice. Raise CELMIS_JOB_LEASE_SECONDS (now %ds) "
                "if this handler legitimately runs longer.",
                job_id, kind, jq.lease_seconds(),
            )
            return


__all__ = ["start_worker", "register"]
