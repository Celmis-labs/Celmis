"""Job queue endpoints.

    GET    /api/jobs              — list (filter by status/kind)
    GET    /api/jobs/stats        — counts per status
    POST   /api/jobs              — manual enqueue (GLOBAL admin only)
    POST   /api/jobs/{id}/retry   — reset dead/failed row to pending
    POST   /api/jobs/{id}/cancel  — cooperative cancel of a running row
    DELETE /api/jobs/{id}         — delete a row

Who sees what
-------------
This was global-admin-only, which meant the person who owns a workspace
could not tell whether their own repository was still indexing. It is now
scoped instead of gated:

    global admin      every row, every tenant
    everyone else     rows whose workspace_id equals THEIR active workspace

Rows with no workspace_id — ownership rebuilds, cross-repo materialize, the
queue-wide maintenance that belongs to nobody — stay global-admin-only. The
filter is `workspace_id = :ws`, which never matches NULL, so that is the
default rather than a rule somebody has to remember. The Jobs page prints a
line saying those rows exist and are hidden; a queue that quietly drops rows
is worse than one that admits it.

Writes are narrower than reads, per the app's permission model: reading your
workspace's own figures is any member, changing its things is owner/admin of
that workspace. Retry/cancel/delete therefore need `require_workspace_admin`
AND a row that belongs to the caller's active workspace.

POST /api/jobs stays GLOBAL admin. It takes an arbitrary kind and an
arbitrary payload, so it can mint a job that names any repo in any tenant —
`{"kind": "review", "payload": {"repo": "<someone else>"}}`. That is not a
workspace setting, it is a way around every check in this file.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from src.api.deps import (
    current_workspace_id,
    get_current_user,
    require_admin,
    require_workspace_admin,
)
from src.sync import queue as jq
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobIn(BaseModel):
    kind: str = Field(min_length=1, max_length=100)
    payload: dict = Field(default_factory=dict)
    dedup_key: str | None = None
    delay_seconds: float = 0
    max_attempts: int = 5
    model_config = ConfigDict(extra="forbid")


def _read_scope(user: User, workspace_id: str) -> str | None:
    """None = no tenant filter (global admin). Otherwise the caller's own
    active workspace, and nothing else."""
    return None if user.is_admin else workspace_id


def _authorize_write(job_id: str, user: User, workspace_id: str) -> dict[str, Any]:
    """The row, once the caller is allowed to act on it.

    404 rather than 403 when the row belongs to another tenant: a 403 would
    confirm that this job id exists, which is itself a fact about somebody
    else's queue.
    """
    job = jq.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if user.is_admin:
        return job
    row_ws = job.get("workspace_id")
    if not row_ws or row_ws != workspace_id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("")
def list_jobs(
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> list[dict[str, Any]]:
    return _serialize_all(jq.list_jobs(
        status=status, kind=kind, limit=limit,
        workspace_id=_read_scope(user, workspace_id),
    ))


@router.get("/stats")
def stats(
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, int]:
    return jq.stats(workspace_id=_read_scope(user, workspace_id))


@router.post("", status_code=201)
def create(payload: JobIn, user: User = Depends(require_admin)) -> dict[str, Any]:
    """Manual enqueue. GLOBAL admin — see the module docstring: an arbitrary
    kind plus an arbitrary payload is a job about any tenant."""
    job_id = jq.enqueue(
        kind=payload.kind,
        payload=payload.payload,
        dedup_key=payload.dedup_key,
        delay_seconds=payload.delay_seconds,
        max_attempts=payload.max_attempts,
        enqueued_by=user.email,
    )
    return {"id": job_id, "deduped": job_id is None}


@router.post("/{job_id}/retry")
def retry(
    job_id: str,
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    _authorize_write(job_id, user, workspace_id)
    ok = jq.retry_dead(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="job is not in a retryable state")
    logger.info("job_retry id=%s by=%s", job_id, user.email)
    return {"ok": True}


@router.post("/{job_id}/cancel")
def cancel(
    job_id: str,
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Request cooperative cancellation of a RUNNING job. The handler stops
    at its next checkpoint (between modules / repos / phases); the row then
    shows as 'cancelled' and can be retried later."""
    _authorize_write(job_id, user, workspace_id)
    ok = jq.request_cancel(job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="job is not running")
    logger.info("job_cancel_requested id=%s by=%s", job_id, user.email)
    return {"ok": True}


@router.delete("/{job_id}", status_code=204)
def delete_job(
    job_id: str,
    user: User = Depends(require_workspace_admin),
    workspace_id: str = Depends(current_workspace_id),
) -> None:
    _authorize_write(job_id, user, workspace_id)
    jq.delete_job(job_id)
    logger.info("job_deleted id=%s by=%s", job_id, user.email)


def _serialize_all(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        item = dict(r)
        for key in ("next_run_at", "locked_until", "created_at",
                    "started_at", "finished_at"):
            v = item.get(key)
            if v is not None and hasattr(v, "isoformat"):
                item[key] = v.isoformat()
        out.append(item)
    return out


__all__ = ["router"]
