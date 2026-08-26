"""Postgres-backed durable job queue.

Design (10-repo / 3-team scale):

  * `sync_jobs` table (see Alembic Stage 18).
  * Enqueue = `INSERT`. Optional `dedup_key` — unique partial index
    on `(dedup_key) WHERE status IN ('pending','running')` prevents a
    second identical job from queueing while one is pending/running.
    Fires a `NOTIFY sync_jobs_new` so workers can wake early (nice-to-
    have; workers also poll every N seconds).
  * Dequeue = `SELECT ... FOR UPDATE SKIP LOCKED` — multiple workers
    coexist safely; each grabs one pending row whose `next_run_at <=
    now()`, marks it `running`, and processes it.
  * Retry = on handler exception the row is bumped back to `pending`,
    `attempts++`, `next_run_at = now() + 2**attempts * base_seconds`
    (capped). At `max_attempts` the row goes to `dead` — surfaced in
    the admin UI for human intervention.
  * Lease timeout — if worker crashes mid-job, `locked_until` expiry
    reclaims the row for another worker on next dequeue.

At our scale this beats Rabbit/Redis on ops cost (zero new services).
Scales to thousands of jobs/day per Postgres box.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.db.session import get_database_url

logger = logging.getLogger(__name__)


# ─── Job kinds — the discriminator that picks the handler ────────────

KIND_REVIEW = "review"
KIND_INDEX_REPO = "index_repo"
#: Distinct from KIND_INDEX_REPO on purpose. That one is the INCREMENTAL pass —
#: it diffs against an existing clone and returns "skipped: clone missing" for
#: a repo nobody has cloned yet, which is exactly the case "Index all" exists
#: to serve. This kind clones and indexes from scratch.
KIND_INDEX_REPO_FULL = "index_repo_full"
KIND_OWNERSHIP_REBUILD = "ownership_rebuild"
KIND_CROSS_REPO_MATERIALIZE = "cross_repo_materialize"
KIND_REINDEX_QDRANT = "reindex_qdrant"
KIND_REGENERATE_NOTES = "regenerate_notes"
KIND_GENERATE_VAULT = "generate_vault"
KIND_DEPS_AUDIT = "deps_audit"
#: Reading a sentence into a plan. It is here rather than in the request
#: because a person who navigates away mid-thought should not lose the
#: reading — and because a job is the only thing in this system that can be
#: asked to stop.
KIND_AUTOMATION_PLAN = "automation_plan"

_LEASE_SECONDS = int(os.environ.get("CELMIS_JOB_LEASE_SECONDS", "600"))
_BACKOFF_BASE = float(os.environ.get("CELMIS_JOB_BACKOFF_BASE", "5"))
_BACKOFF_CAP = float(os.environ.get("CELMIS_JOB_BACKOFF_CAP_SECONDS", "1800"))


class JobCancelled(Exception):
    """Raised inside a handler when the operator requested cancellation.

    Long handlers poll `is_cancel_requested(job_id)` between units of work
    and raise this; the worker marks the row 'cancelled' (retryable from
    the Jobs page) instead of counting it as a failure."""


# ─── Engine cache (sync) ────────────────────────────────────────────


_ENGINE: Engine | None = None


def _engine() -> Engine:
    global _ENGINE
    if _ENGINE is None:
        url = get_database_url().replace(
            "postgresql+asyncpg://", "postgresql+psycopg://"
        )
        _ENGINE = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    return _ENGINE


#: Which of the `CELMIS_SYNC_WORKER_CONCURRENCY` loops in THIS process is
#: acting. A ContextVar and not a global because the loops are asyncio tasks
#: sharing one interpreter: `asyncio.gather` gives each task its own copy of
#: the context, and `asyncio.to_thread` — how the loops reach this
#: synchronous module — copies that context into the thread.
_SLOT: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "celmis_worker_slot", default=None,
)


def set_worker_slot(slot: int) -> None:
    """Called once by each worker loop, so `locked_by` names a worker."""
    _SLOT.set(slot)


def _worker_id() -> str:
    """Identify the WORKER, not the process it happens to live in.

    This used to be `hostname-pid`, which is the same string for every worker
    loop in one container — and the default concurrency is 2. So `locked_by`
    answered "which process holds this job", a question nobody asked, while
    the question it is named for ("which worker") had no answer at all.

    That is not cosmetic. It made every `locked_by = :w` guard vacuous between
    siblings: when loop 1 reclaimed loop 0's expired lease, the row's owner
    string did not change, so loop 0 could go on renewing — and later
    completing — a job loop 1 was by then running. A guard that cannot fail is
    not a guard.
    """
    slot = _SLOT.get()
    base = f"{socket.gethostname()}-{os.getpid()}"
    return base if slot is None else f"{base}-{slot}"


# ─── Enqueue ────────────────────────────────────────────────────────


def _normalize_workspace(value: Any) -> str | None:
    """A workspace id or None. Empty strings and non-strings are None —
    a blank tenant must never become a tenant somebody can match on."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def enqueue(
    *,
    kind: str,
    payload: dict[str, Any],
    dedup_key: str | None = None,
    delay_seconds: float = 0,
    max_attempts: int = 5,
    enqueued_by: str | None = None,
    workspace_id: str | None = None,
) -> str | None:
    """Insert a job. Returns the row id, or None if a matching pending/
    running job already exists (dedup hit).

    `workspace_id` is the tenant the row belongs to and is what the Jobs
    page filters on. When it is not passed it falls back to
    `payload["workspace_id"]`, which most callers already carry — the same
    rule the backfill migration used, so old rows and new rows agree.

    Leave it None for work that genuinely has no tenant (ownership rebuild,
    cross-repo materialize): such rows are visible to global admins only.
    Guessing here is worse than admitting it — a wrong guess shows one
    tenant's repository to another.
    """
    now = datetime.now(UTC)
    run_at = now + timedelta(seconds=max(0, delay_seconds))
    job_id = str(uuid.uuid4())
    ws = _normalize_workspace(workspace_id) or _normalize_workspace(
        payload.get("workspace_id")
    )
    with _engine().begin() as conn:
        if dedup_key:
            row = conn.execute(text(
                "SELECT id FROM sync_jobs "
                "WHERE dedup_key = :k AND status IN ('pending','running') LIMIT 1"
            ), {"k": dedup_key}).fetchone()
            if row:
                logger.info("job_enqueue_deduped kind=%s dedup=%s existing=%s",
                            kind, dedup_key, row[0])
                return None
        conn.execute(text(
            "INSERT INTO sync_jobs "
            "(id, kind, workspace_id, dedup_key, payload, status, next_run_at, "
            " max_attempts, enqueued_by) "
            "VALUES (:id, :k, :ws, :d, CAST(:p AS jsonb), 'pending', :r, :m, :by)"
        ), {
            "id": job_id, "k": kind, "ws": ws, "d": dedup_key,
            "p": json.dumps(payload),
            "r": run_at, "m": max_attempts, "by": enqueued_by,
        })
        # Best-effort NOTIFY — workers listen for wake-up.
        # Not supported outside Postgres (sqlite in tests); the poll loop covers it.
        with contextlib.suppress(Exception):
            conn.execute(text("NOTIFY sync_jobs_new"))
    logger.info("job_enqueued id=%s kind=%s ws=%s dedup=%s",
                job_id, kind, ws or "-", dedup_key)
    return job_id


# ─── Dequeue / lease ────────────────────────────────────────────────


def dequeue_one() -> dict[str, Any] | None:
    """Atomically lease one pending job. Returns dict or None if idle.

    Uses SKIP LOCKED so multiple workers can safely poll the same table.
    """
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=_LEASE_SECONDS)
    worker = _worker_id()
    with _engine().begin() as conn:
        # First reclaim expired leases: rows still in 'running' whose
        # locked_until has passed → mark pending again.
        conn.execute(text(
            "UPDATE sync_jobs SET status = 'pending', locked_by = NULL, "
            "  locked_until = NULL "
            "WHERE status = 'running' AND locked_until < :now"
        ), {"now": now})

        row = conn.execute(text(
            "SELECT id, kind, payload, attempts, max_attempts "
            "FROM sync_jobs "
            "WHERE status = 'pending' AND next_run_at <= :now "
            "ORDER BY next_run_at "
            "LIMIT 1 FOR UPDATE SKIP LOCKED"
        ), {"now": now}).mappings().fetchone()
        if row is None:
            return None
        conn.execute(text(
            "UPDATE sync_jobs SET status = 'running', "
            "  locked_by = :w, locked_until = :until, "
            "  started_at = COALESCE(started_at, :now), "
            "  attempts = attempts + 1, cancel_requested = FALSE "
            "WHERE id = :id"
        ), {"w": worker, "until": lease_until, "now": now, "id": row["id"]})
    return dict(row)


def lease_seconds() -> float:
    """The lease width, read through a function so a caller that must pace
    itself against it cannot hardcode a second copy of the number."""
    return float(_LEASE_SECONDS)


def renew_lease(job_id: str) -> bool:
    """Push this worker's lease on a running job forward. True if we still hold it.

    THE LEASE WAS A GUESS ABOUT DURATION, AND THE GUESS WAS WRONG.

    `dequeue_one` stamped `locked_until = now + 600s` once and nothing ever
    moved it, so the row said "a worker is on this" for exactly ten minutes
    regardless of whether one was. Past that, the reclaim at the top of
    `dequeue_one` handed the job to the next worker while the first was still
    inside the handler — and with `CELMIS_SYNC_WORKER_CONCURRENCY` at its
    default of 2, the next worker is a live sibling loop in the same process.

    That is not hypothetical. Measured over 517 real reviews on this install:
    3.5% ran longer than 600 seconds (p99 1486s, max 1578s). Roughly one review
    in twenty-nine was leased out from under itself and run a second time —
    twice the LLM spend, two run rows, and two workers racing to post comments
    on one pull request.

    A lease should mean "a worker is alive on this", which is a fact a worker
    can report, not a duration anyone can predict. `locked_by` is in the WHERE
    clause on purpose: a worker that already lost the row must not silently
    take it back from whoever owns it now.
    """
    with _engine().begin() as conn:
        result = conn.execute(text(
            "UPDATE sync_jobs SET locked_until = :until "
            "WHERE id = :id AND status = 'running' AND locked_by = :w"
        ), {
            "until": datetime.now(UTC) + timedelta(seconds=_LEASE_SECONDS),
            "id": job_id, "w": _worker_id(),
        })
    return bool(result.rowcount)


def mark_complete(job_id: str) -> None:
    with _engine().begin() as conn:
        conn.execute(text(
            "UPDATE sync_jobs SET status='completed', finished_at=:now, "
            "  locked_by=NULL, locked_until=NULL "
            "WHERE id = :id"
        ), {"now": datetime.now(UTC), "id": job_id})


def mark_failure(job_id: str, err: str, *, attempts: int, max_attempts: int) -> None:
    now = datetime.now(UTC)
    if attempts >= max_attempts:
        with _engine().begin() as conn:
            conn.execute(text(
                "UPDATE sync_jobs SET status='dead', last_error=:e, "
                "  finished_at=:now, locked_by=NULL, locked_until=NULL "
                "WHERE id = :id"
            ), {"e": err[:2000], "now": now, "id": job_id})
        logger.warning("job_dead id=%s attempts=%d err=%s", job_id, attempts, err[:200])
        return
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (attempts - 1)))
    with _engine().begin() as conn:
        conn.execute(text(
            "UPDATE sync_jobs SET status='pending', last_error=:e, "
            "  next_run_at=:next, locked_by=NULL, locked_until=NULL "
            "WHERE id = :id"
        ), {"e": err[:2000], "next": now + timedelta(seconds=delay), "id": job_id})
    logger.info("job_retry_scheduled id=%s attempts=%d delay=%.1fs", job_id, attempts, delay)


def mark_cancelled(job_id: str, note: str = "cancelled by user") -> None:
    with _engine().begin() as conn:
        conn.execute(text(
            "UPDATE sync_jobs SET status='cancelled', last_error=:e, "
            "  finished_at=:now, locked_by=NULL, locked_until=NULL, "
            "  cancel_requested=FALSE "
            "WHERE id = :id"
        ), {"e": note[:2000], "now": datetime.now(UTC), "id": job_id})
    logger.info("job_cancelled id=%s", job_id)


# ─── Cooperative cancellation ────────────────────────────────────────


def request_cancel(job_id: str) -> bool:
    """Flag a running job for cancellation. The handler notices at its next
    checkpoint (between repos / modules / phases) — this is cooperative, not
    a kill. Returns False when the job isn't running."""
    with _engine().begin() as conn:
        r = conn.execute(text(
            "UPDATE sync_jobs SET cancel_requested = TRUE "
            "WHERE id = :id AND status = 'running'"
        ), {"id": job_id})
        return r.rowcount > 0


def is_cancel_requested(job_id: str) -> bool:
    with _engine().begin() as conn:
        row = conn.execute(text(
            "SELECT cancel_requested FROM sync_jobs WHERE id = :id"
        ), {"id": job_id}).fetchone()
    return bool(row and row[0])


# ─── Admin helpers ───────────────────────────────────────────────────


def list_jobs(
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Rows newest-first.

    `workspace_id=None` means NO tenant filter — reserved for global
    admins. Pass a workspace id and the result is exactly that tenant's
    rows: `workspace_id = :ws` never matches NULL, so untenanted
    maintenance rows drop out without a special case. That is the
    fail-closed direction — a row whose owner we do not know is not shown
    to somebody who might not own it.
    """
    q = "SELECT * FROM sync_jobs"
    conds: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if status:
        conds.append("status = :status")
        params["status"] = status
    if kind:
        conds.append("kind = :kind")
        params["kind"] = kind
    ws = _normalize_workspace(workspace_id)
    if ws:
        conds.append("workspace_id = :ws")
        params["ws"] = ws
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY next_run_at DESC LIMIT :limit"
    with _engine().begin() as conn:
        return [dict(r) for r in conn.execute(text(q), params).mappings().all()]


def get_job(job_id: str) -> dict[str, Any] | None:
    """One row, or None. The caller needs `workspace_id` off it before it
    may retry/cancel/delete on somebody's behalf."""
    with _engine().begin() as conn:
        row = conn.execute(
            text("SELECT * FROM sync_jobs WHERE id = :id"), {"id": job_id},
        ).mappings().fetchone()
    return dict(row) if row is not None else None


def retry_dead(job_id: str) -> bool:
    now = datetime.now(UTC)
    with _engine().begin() as conn:
        r = conn.execute(text(
            "UPDATE sync_jobs SET status='pending', attempts=0, "
            "  next_run_at=:now, last_error=NULL, cancel_requested=FALSE "
            "WHERE id = :id AND status IN ('dead','failed','cancelled')"
        ), {"now": now, "id": job_id})
        return r.rowcount > 0


def delete_job(job_id: str) -> bool:
    with _engine().begin() as conn:
        r = conn.execute(text("DELETE FROM sync_jobs WHERE id = :id"), {"id": job_id})
        return r.rowcount > 0


def stats(*, workspace_id: str | None = None) -> dict[str, int]:
    """Counts per status. Scoped like `list_jobs` — the tiles above the
    list must not count rows the list refuses to show, or the page lies."""
    q = "SELECT status, COUNT(*) AS n FROM sync_jobs"
    params: dict[str, Any] = {}
    ws = _normalize_workspace(workspace_id)
    if ws:
        q += " WHERE workspace_id = :ws"
        params["ws"] = ws
    q += " GROUP BY status"
    with _engine().begin() as conn:
        rows = conn.execute(text(q), params).all()
    return {r[0]: int(r[1]) for r in rows}


__all__ = [
    "KIND_REVIEW", "KIND_INDEX_REPO", "KIND_OWNERSHIP_REBUILD",
    "KIND_CROSS_REPO_MATERIALIZE", "KIND_REINDEX_QDRANT",
    "KIND_REGENERATE_NOTES",
    "JobCancelled",
    "enqueue", "dequeue_one", "mark_complete", "mark_failure",
    "mark_cancelled", "request_cancel", "is_cancel_requested",
    "list_jobs", "get_job", "retry_dead", "delete_job", "stats",
]
