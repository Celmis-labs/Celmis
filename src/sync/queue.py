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
  * Deferral = a handler raising `JobDeferred` (the review model is down)
    parks the row as `deferred`: no attempt consumed, dedup still holds, a
    processor in the worker releases it when the dependency answers again,
    and it goes `dead` after CELMIS_REVIEW_DEFER_MAX_HOURS.
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

from sqlalchemy import bindparam, create_engine, text
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
#: One `@celmis ...` pull-request comment, already accepted by the receiver
#: (verified, bound to a workspace, claimed in the command ledger). The handler
#: answers it on the pull request; see `src.review.commands.handlers`.
KIND_PR_COMMAND = "pr_command"
#: Productivity history of one repository (src/productivity/sync.py): a time-boxed
#: slice that queues its own continuation while work remains.
KIND_PRODUCTIVITY_SYNC = "productivity_sync"
#: One PR re-read after its merge or close webhook.
KIND_PRODUCTIVITY_PR = "productivity_pr"

STATUS_DEFERRED = "deferred"

#: Seconds between two checks of a deferred job, by how many checks already
#: failed: 1, 2, 5, 10, then 15 minutes for as long as the outage lasts.
_DEFER_INTERVALS = (60, 120, 300, 600, 900)
#: How long a processor owns the jobs it picked up to probe for, so a sibling
#: replica's tick does not pick the same rows (a crashed one frees them again).
_DEFER_CLAIM_SECONDS = 120

_LEASE_SECONDS = int(os.environ.get("CELMIS_JOB_LEASE_SECONDS", "600"))
_BACKOFF_BASE = float(os.environ.get("CELMIS_JOB_BACKOFF_BASE", "5"))
_BACKOFF_CAP = float(os.environ.get("CELMIS_JOB_BACKOFF_CAP_SECONDS", "1800"))


class JobDeferred(Exception):
    """Raised by a handler that cannot do its work NOW for a reason that is
    nobody's fault and will pass — the review model is down.

    The worker parks the row as 'deferred' instead of completing or failing
    it: no attempt is consumed (an outage is not the job's fault), and a
    processor releases it when the dependency answers again. `reason` is
    shown in the UI, so it must be a curated sentence — never an exception's
    own text, never a secret. `code` is a stable slug for tests and filters."""

    def __init__(self, reason: str, *, code: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


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
                "WHERE dedup_key = :k "
                "AND status IN ('pending','running','deferred') LIMIT 1"
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


def _claimed_attempt(row: dict[str, Any]) -> dict[str, Any]:
    """The row as the worker should see it: `attempts` is THIS attempt.

    `claim` SELECTs before it increments, so the row it reads carries the
    count from before this run. Handing that number onward made the first
    attempt `0`, the first backoff half the base delay, and — because
    `mark_failure` stops at `attempts >= max_attempts` — the fifth run of a
    five-attempt job report four and retry. Five means five.

    A copy, not an edit: the mapping the driver returned is also what the
    UPDATE above was built from, and two readers disagreeing about the same
    row is the shape of the bug this replaces.
    """
    return {**row, "attempts": row["attempts"] + 1}


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
    return _claimed_attempt(dict(row))


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


def is_deferred(dedup_key: str) -> bool:
    """True when the job holding `dedup_key` is waiting ('deferred'). Never
    raises: a failed lookup only costs a less specific message."""
    try:
        with _engine().begin() as conn:
            row = conn.execute(text(
                "SELECT 1 FROM sync_jobs WHERE dedup_key = :k "
                "AND status = 'deferred' LIMIT 1"), {"k": dedup_key}).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def cancel_deferred(job_id: str, note: str = "cancelled by an administrator"
                    ) -> dict[str, Any] | None:
    """Cancel a job that is waiting ('deferred'). Returns the row (its payload,
    for closing the run) when this call cancelled it, None when it was not
    deferred. Conditional, so it never races the deferred-list processor."""
    with _engine().begin() as conn:
        row = conn.execute(text(
            "UPDATE sync_jobs SET status='cancelled', last_error=:e, "
            "  finished_at=:now, locked_by=NULL, locked_until=NULL, "
            "  cancel_requested=FALSE "
            "WHERE id = :id AND status = 'deferred' "
            "RETURNING id, kind, workspace_id, payload"
        ), {"e": note[:2000], "now": datetime.now(UTC), "id": job_id}).mappings().fetchone()
    if row is None:
        return None
    logger.info("job_cancelled id=%s was=deferred", job_id)
    return dict(row)


def retry_dead(job_id: str) -> bool:
    """Run a job again now. A dead/failed/cancelled row starts over (attempts
    and deferral state cleared); a deferred one just stops waiting — its
    attempts were never consumed."""
    now = datetime.now(UTC)
    with _engine().begin() as conn:
        r = conn.execute(text(
            "UPDATE sync_jobs SET status='pending', attempts=0, "
            "  next_run_at=:now, last_error=NULL, cancel_requested=FALSE, "
            "  deferred_since=NULL, defer_reason=NULL, defer_code=NULL, "
            "  defer_checks=0 "
            "WHERE id = :id AND status IN ('dead','failed','cancelled')"
        ), {"now": now, "id": job_id})
        if r.rowcount > 0:
            return True
        r = conn.execute(text(
            "UPDATE sync_jobs SET status='pending', next_run_at=:now, "
            "  defer_checks=0 "
            "WHERE id = :id AND status = 'deferred'"
        ), {"now": now, "id": job_id})
        return r.rowcount > 0


# ─── Deferral: wait for a dependency instead of failing ──────────────


def defer_interval(checks: int, *, floor: float = 0) -> float:
    """Seconds until the next check after `checks` failed ones (0-based)."""
    i = max(0, min(int(checks), len(_DEFER_INTERVALS) - 1))
    return max(float(floor), float(_DEFER_INTERVALS[i]))


def mark_deferred(job_id: str, reason: str, *, code: str | None = None,
                  floor: float = 0) -> bool:
    """Park a running job as 'deferred'. True when this is the job's FIRST
    deferral (the caller logs WARNING once per job, not once per outage tick).

    The attempt `dequeue_one` counted is handed back: an outage is not the
    job's fault, so a deferral never moves it towards 'dead'. `deferred_since`
    keeps the first deferral (the expiry clock); the first probe is due after
    the shortest interval."""
    now = datetime.now(UTC)
    with _engine().begin() as conn:
        row = conn.execute(text(
            "SELECT deferred_since, defer_checks FROM sync_jobs "
            "WHERE id = :id AND status = 'running' AND locked_by = :w"),
            {"id": job_id, "w": _worker_id()}).fetchone()
        if row is None:
            # Not ours any more (lease expired, another worker took the row):
            # a late deferral must not park somebody else's run.
            return False
        first = row[0] is None
        # A job released and deferred AGAIN counts that as one more failed
        # check, so a request that times out whatever the probe says does not
        # cycle at the shortest interval for a day.
        checks = 0 if first else int(row[1] or 0) + 1
        conn.execute(text(
            "UPDATE sync_jobs SET status='deferred', "
            "  attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END, "
            "  deferred_since = COALESCE(deferred_since, :now), "
            "  defer_reason = :why, defer_code = :code, defer_checks = :c, "
            "  next_run_at = :next, locked_by=NULL, locked_until=NULL "
            "WHERE id = :id"
        ), {"now": now, "why": reason[:500], "code": code, "id": job_id, "c": checks,
            "next": now + timedelta(seconds=defer_interval(checks, floor=floor))})
    if first:
        logger.warning("job_deferred id=%s code=%s reason=%s", job_id, code or "-",
                       reason[:200])
    else:
        logger.info("job_deferred_again id=%s code=%s", job_id, code or "-")
    return first


def claim_due_deferred(kind: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Deferred jobs whose next check is due, claimed for THIS processor.

    Each row is claimed by its own conditional UPDATE (status still deferred,
    still due), which Postgres serialises per row: exactly one of two
    concurrent processors wins a given row, and the loser sees rowcount 0.
    The claim pushes `next_run_at` out by `_DEFER_CLAIM_SECONDS`, so the rows
    are not due for anybody else while the winner probes — and are due again
    if it dies."""
    now = datetime.now(UTC)
    until = now + timedelta(seconds=_DEFER_CLAIM_SECONDS)
    with _engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT id, kind, workspace_id, payload, deferred_since, defer_checks "
            "FROM sync_jobs WHERE status = 'deferred' AND kind = :k "
            "AND next_run_at <= :now ORDER BY deferred_since, id LIMIT :n"
        ), {"k": kind, "now": now, "n": limit}).mappings().all()
        won: list[dict[str, Any]] = []
        for row in rows:
            r = conn.execute(text(
                "UPDATE sync_jobs SET next_run_at = :until "
                "WHERE id = :id AND status = 'deferred' AND next_run_at <= :now"
            ), {"until": until, "id": row["id"], "now": now})
            if r.rowcount:
                d = dict(row)
                if isinstance(d.get("payload"), str):
                    d["payload"] = json.loads(d["payload"])
                won.append(d)
    return won


def release_deferred(job_ids: list[str]) -> int:
    """Move claimed deferred jobs back to 'pending', oldest first. Returns how
    many this call released — a job somebody else already released (manual
    retry, a sibling replica) is not counted twice."""
    now = datetime.now(UTC)
    released = 0
    with _engine().begin() as conn:
        for i, job_id in enumerate(job_ids):
            r = conn.execute(text(
                "UPDATE sync_jobs SET status='pending', next_run_at=:at, "
                "  defer_reason=NULL, defer_code=NULL "
                "WHERE id = :id AND status = 'deferred'"
            ), {"at": now + timedelta(milliseconds=i), "id": job_id})
            released += int(r.rowcount or 0)
        with contextlib.suppress(Exception):
            conn.execute(text("NOTIFY sync_jobs_new"))
    return released


def reschedule_deferred(jobs: list[dict[str, Any]], *, reason: str | None = None,
                        code: str | None = None, floor: float = 0) -> None:
    """The dependency is still down: one more failed check for each job, and
    the next check later (1, 2, 5, 10, 15 minutes). `jobs` are rows from
    `claim_due_deferred`."""
    now = datetime.now(UTC)
    with _engine().begin() as conn:
        for j in jobs:
            checks = int(j.get("defer_checks") or 0) + 1
            conn.execute(text(
                "UPDATE sync_jobs SET defer_checks = :c, next_run_at = :next, "
                "  defer_reason = COALESCE(:why, defer_reason), "
                "  defer_code = COALESCE(:code, defer_code) "
                "WHERE id = :id AND status = 'deferred'"
            ), {"c": checks, "id": j["id"], "why": reason[:500] if reason else None,
                "code": code,
                "next": now + timedelta(seconds=defer_interval(checks, floor=floor))})


def expire_deferred(kind: str, *, max_age: timedelta) -> list[dict[str, Any]]:
    """Deferred jobs older than `max_age` (since the FIRST deferral) go dead.
    Returns the rows this call buried, for the caller to close their run
    records; a row buried by a sibling replica is not returned twice."""
    now = datetime.now(UTC)
    cutoff = now - max_age
    with _engine().begin() as conn:
        rows = conn.execute(text(
            "SELECT id, kind, workspace_id, payload, deferred_since, defer_reason "
            "FROM sync_jobs WHERE status = 'deferred' AND kind = :k "
            "AND deferred_since IS NOT NULL AND deferred_since < :cut"
        ), {"k": kind, "cut": cutoff}).mappings().all()
        buried: list[dict[str, Any]] = []
        for row in rows:
            msg = f"model unavailable for {max_age.total_seconds() / 3600:g} h"
            r = conn.execute(text(
                "UPDATE sync_jobs SET status='dead', last_error=:e, finished_at=:now, "
                "  locked_by=NULL, locked_until=NULL "
                "WHERE id = :id AND status = 'deferred'"
            ), {"e": msg, "now": now, "id": row["id"]})
            if r.rowcount:
                d = dict(row)
                d["reason"] = msg
                if isinstance(d.get("payload"), str):
                    d["payload"] = json.loads(d["payload"])
                buried.append(d)
    return buried


def deferred_for_runs(run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """{run_id: the deferred review job of that run} — for the runs list, which
    shows why a run waits and lets a workspace admin retry it now."""
    ids = [r for r in run_ids if r]
    if not ids:
        return {}
    stmt = text(
        "SELECT id, deferred_since, next_run_at, defer_reason, "
        "  payload->>'run_id' AS run_id FROM sync_jobs "
        "WHERE status = 'deferred' AND kind = :k AND payload->>'run_id' IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    with _engine().begin() as conn:
        rows = conn.execute(stmt, {"k": KIND_REVIEW, "ids": ids}).mappings().all()
    return {r["run_id"]: dict(r) for r in rows}


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
    "KIND_REGENERATE_NOTES", "KIND_PR_COMMAND",
    "JobCancelled",
    "JobDeferred", "STATUS_DEFERRED", "cancel_deferred", "is_deferred",
    "deferred_for_runs", "mark_deferred", "claim_due_deferred",
    "release_deferred", "reschedule_deferred", "expire_deferred", "defer_interval",
    "enqueue", "dequeue_one", "mark_complete", "mark_failure",
    "mark_cancelled", "request_cancel", "is_cancel_requested",
    "list_jobs", "get_job", "retry_dead", "delete_job", "stats",
]
