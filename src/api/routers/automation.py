"""Read a sentence, run what was approved, and remember both.

Split in two on purpose. Interpretation is a guess and these verbs cost hours
of model time, so nothing runs on the first press: `/plan` says what it
understood and which repositories that resolves to, and `/execute` runs a plan
a person has seen. "All of them" meaning forty repositories instead of four is
obvious in a list and invisible in a sentence.

Both halves are written down. The page used to keep the question and the plan
in React state alone, so navigating away discarded them — while the work
carried on in the background, queued and running, with nothing on screen to
say so. A row is written when the sentence is READ, not when it is confirmed,
because "what did I ask, and did I press the button" is precisely the question
somebody has when they come back.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.db.models import AutomationRun
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/automation", tags=["automation"])


class PlanIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    #: Which conversation this belongs to. Chosen by the client — the agent
    #: reads every sentence on its own, so a session groups for reading back
    #: rather than feeding context, and the browser is the only thing that
    #: knows where one sitting ends and the next begins.
    session_id: str | None = Field(default=None, max_length=64)


class StepOut(BaseModel):
    action: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    note: str = ""
    resolved_repos: list[str] = Field(default_factory=list)
    blocked: str | None = None


class RunOut(BaseModel):
    """One row: the question, what it was read as, and what came of it.

    The same shape whether it is still being read, waiting for a press, or
    finished — the page polls one endpoint and renders one thing rather than
    holding a state machine of its own.
    """

    id: str
    session_id: str | None = None
    message: str
    #: reading | planned | answered | started | failed | stopped
    status: str
    steps: list[StepOut] = Field(default_factory=list)
    note: str = ""
    #: The note as it is being WRITTEN, while `status` is "reading". The
    #: planner streams and is asked for the sentence before the steps, so
    #: there is something to read a second in rather than after the whole plan
    #: exists. Empty on a finished run — `note` is the authority then.
    partial_note: str = ""
    #: The language the question was asked in. The client shows the canned
    #: parts of the answer in it rather than in the interface language.
    language: str = ""
    resolved_repos: list[str] = Field(default_factory=list)
    blocked: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    asked_by: str | None = None
    created_at: str = ""
    executed_at: str | None = None


def _as_run(row: AutomationRun) -> RunOut:
    return RunOut(
        id=row.id,
        session_id=row.session_id,
        message=row.message,
        status=row.status,
        steps=[StepOut(**st) for st in (row.steps or [])],
        note=row.note or "",
        # getattr, not attribute access: rows built before the column existed
        # reach this mapper in tests and in a mid-deploy request.
        partial_note=getattr(row, "partial_note", None) or "",
        language=row.language or "",
        resolved_repos=list(row.resolved_repos or []),
        blocked=row.blocked,
        result=row.result or {},
        error=row.error,
        asked_by=row.user_email,
        created_at=row.created_at.isoformat() if row.created_at else "",
        executed_at=row.executed_at.isoformat() if row.executed_at else None,
    )


@router.post("/plan", response_model=RunOut, status_code=202)
async def plan(
    payload: PlanIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    session: AsyncSession = Depends(get_async_session),
) -> RunOut:
    """Start reading the sentence. Returns before the reading is done.

    The reading used to happen inside this request, which is why leaving the
    page lost it: the answer was computed and dropped, and pressing again paid
    for the same model call twice. It is a queue job now — so it survives the
    browser, survives an API restart, and can be told to stop.
    """
    from src.sync.queue import KIND_AUTOMATION_PLAN, enqueue

    row = AutomationRun(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        user_id=user.id,
        user_email=user.email,
        message=payload.message,
        session_id=payload.session_id,
        status="reading",
    )
    session.add(row)
    await session.commit()

    job_id = enqueue(
        kind=KIND_AUTOMATION_PLAN,
        payload={
            "run_id": row.id, "message": payload.message,
            "workspace_id": workspace_id, "user_id": user.id,
            "user_email": user.email,
        },
        # One attempt. A retry would charge for the same sentence twice and
        # the person is watching a spinner that has to end either way.
        max_attempts=1,
        enqueued_by=user.email,
    )
    if job_id is None:
        # No dedup key is passed, so this is a queue that refused the insert.
        row.status = "failed"
        row.error = "Could not queue the reading."
        await session.commit()
        return _as_run(row)

    row.job_id = job_id
    await session.commit()
    logger.info("automation_plan_queued run=%s job=%s ws=%s by=%s",
                row.id, job_id, workspace_id, user.email)
    return _as_run(row)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    session: AsyncSession = Depends(get_async_session),
) -> RunOut:
    """One row, polled while it is being read."""
    row = await session.get(AutomationRun, run_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="No such run")
    return _as_run(row)


@router.post("/runs/{run_id}/stop", response_model=RunOut)
async def stop_run(
    run_id: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    session: AsyncSession = Depends(get_async_session),
) -> RunOut:
    """Stop a reading in progress.

    Only the reading. Work that has already been approved and queued is not
    cancelled from here: those are the jobs the Monitoring page owns, and
    quietly half-cancelling a documentation sweep from a chat box would leave
    a state nobody can describe.
    """
    from src.sync.queue import mark_cancelled, request_cancel

    row = await session.get(AutomationRun, run_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="No such run")
    if row.status != "reading":
        raise HTTPException(
            status_code=409,
            detail="That is not being read any more.",
        )

    if row.job_id:  # noqa: SIM102 - request_cancel() acts, it is not a test
        # Cooperative first — the handler checks between phases. A job still
        # pending was never picked up, so cancel it outright. Merging the two
        # `if`s would bury that mutation inside a boolean expression.
        if not request_cancel(row.job_id):
            mark_cancelled(row.job_id, "stopped from the agent page")
    row.status = "stopped"
    await session.commit()
    logger.info("automation_plan_stopped run=%s by=%s", row.id, user.email)
    return _as_run(row)


class ExecuteIn(BaseModel):
    """Which stored reading to run.

    The plan used to be posted back from the browser, on the argument that
    what runs must be what the person looked at. It is stored now, which is
    the same argument answered better: the row IS what they looked at, and a
    client can no longer post a scope of its own invention.
    """

    plan_id: str


@router.post("/execute", status_code=202)
async def execute_plan(
    payload: ExecuteIn,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    """Run an approved plan, through the same actions every surface uses."""
    from src.automation.actions import ActionError, Actor
    from src.automation.chat import CATALOGUE, Plan, Step, execute, resolve_scope

    row = await session.get(AutomationRun, payload.plan_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="No such plan")
    if row.status not in ("planned",):
        raise HTTPException(
            status_code=409,
            detail="That plan is not waiting to be run.",
        )

    steps = [
        Step(action=st.get("action"), arguments=dict(st.get("arguments") or {}),
             note=str(st.get("note") or ""))
        for st in (row.steps or [])
    ]
    unknown = [st.action for st in steps if st.action not in CATALOGUE]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"There is no action called {unknown[0]!r}.")

    # Re-resolved rather than replayed. The stored list was correct when it
    # was read; a repository registered since would make "all of them" mean
    # something wider than the person approved, and this is where that is
    # caught.
    plan = resolve_scope(Plan(steps=steps, note=row.note or ""),
                         workspace_id=workspace_id)
    if plan.blocked:
        await _record_outcome(session, row.id, workspace_id,
                              status="failed", error=plan.blocked)
        raise HTTPException(status_code=409, detail=plan.blocked)

    actor = Actor(user_id=user.id, email=user.email,
                  workspace_id=workspace_id, label="chat")
    try:
        result = await execute(plan, actor, session)
    except ActionError as exc:
        await _record_outcome(session, row.id, workspace_id,
                              status="failed", error=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _record_outcome(session, row.id, workspace_id,
                          status="started", result=result)
    return result


async def _record_outcome(
    session: AsyncSession,
    plan_id: str | None,
    workspace_id: str,
    *,
    status: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    """Land the outcome on the row the person was looking at.

    Scoped by workspace as well as id: the id comes from the client, and a row
    from another tenant must not be writable by quoting its uuid.

    Bookkeeping never fails the request. The work is already queued by the
    time this runs, and a 500 here would tell the person nothing started when
    twenty jobs just did.
    """
    if not plan_id:
        return
    try:
        row = await session.get(AutomationRun, plan_id)
        if row is None or row.workspace_id != workspace_id:
            return
        row.status = status
        row.result = result or {}
        row.error = error
        row.executed_at = datetime.now(UTC)
        await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("automation_outcome_not_recorded plan_id=%s", plan_id)


class SessionOut(BaseModel):
    """One conversation, as it appears in a list of previous ones."""

    session_id: str
    #: The first thing asked in it — the only title a session can have that
    #: was not invented. A generated one would cost a model call per session
    #: to say what the person already wrote.
    title: str
    runs: int
    started_at: str
    last_at: str


@router.get("/sessions", response_model=list[SessionOut])
async def sessions(
    limit: int = 30,
    _user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    session: AsyncSession = Depends(get_async_session),
) -> list[SessionOut]:
    """Previous conversations, newest first.

    Rows with no session — everything asked before sessions existed, and
    anything a client posts without one — are grouped by day rather than
    hidden, so the history does not appear to begin the day this shipped.
    """
    from sqlalchemy import func, select

    key = func.coalesce(
        AutomationRun.session_id,
        func.concat("day-", func.to_char(AutomationRun.created_at, "YYYY-MM-DD")),
    ).label("sid")
    rows = (await session.execute(
        select(key,
               func.count().label("runs"),
               func.min(AutomationRun.created_at).label("started"),
               func.max(AutomationRun.created_at).label("last"))
        .where(AutomationRun.workspace_id == workspace_id)
        .group_by(key)
        .order_by(func.max(AutomationRun.created_at).desc())
        .limit(max(1, min(limit, 100)))
    )).all()

    out: list[SessionOut] = []
    for sid, runs, started, last in rows:
        title = (await session.execute(
            select(AutomationRun.message)
            .where(AutomationRun.workspace_id == workspace_id,
                   func.coalesce(
                       AutomationRun.session_id,
                       func.concat("day-", func.to_char(
                           AutomationRun.created_at, "YYYY-MM-DD"))) == sid)
            .order_by(AutomationRun.created_at.asc()).limit(1)
        )).scalar() or ""
        out.append(SessionOut(
            session_id=str(sid), title=title, runs=int(runs),
            started_at=started.isoformat() if started else "",
            last_at=last.isoformat() if last else "",
        ))
    return out


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    session: AsyncSession = Depends(get_async_session),
) -> None:
    """Forget a conversation. Not the work it started.

    Two different things are called "the run" on this page and only one of
    them is deleted here. An `automation_runs` row is a TURN: a sentence, what
    it was read as, and whether it was approved. The jobs `/execute` queued
    are the WORK, they live in the queue, they are owned by Monitoring, and a
    documentation sweep over twenty repositories carries on regardless of who
    tidies up their chat list afterwards. Deleting the transcript of an order
    does not cancel the order.

    Scoped by workspace as well as by id, and the id comes from the client:
    without the workspace in the WHERE clause, anyone holding a uuid could
    delete another tenant's history by quoting it. The day-buckets the
    sessions list invents for pre-session rows are matched by the same
    expression that invents them, so the rows a person can SEE are the rows
    they can remove — a listed conversation that answers 404 is worse than no
    button.
    """
    from sqlalchemy import delete, func, select

    key = func.coalesce(
        AutomationRun.session_id,
        func.concat("day-", func.to_char(AutomationRun.created_at, "YYYY-MM-DD")),
    )
    scope = (AutomationRun.workspace_id == workspace_id, key == session_id)

    # Read first, delete second. Two reasons: after the DELETE there is
    # nothing left to say which readings were in flight, and "did this match
    # anything" is asked of a SELECT rather than of a rowcount, which the
    # driver is entitled to report as -1 when the statement uses RETURNING.
    rows = (await session.execute(
        select(AutomationRun.job_id, AutomationRun.status).where(*scope)
    )).all()
    if not rows:
        raise HTTPException(status_code=404, detail="No such chat")
    reading = [job for job, status in rows if status == "reading" and job]

    await session.execute(
        delete(AutomationRun).where(*scope)
        .execution_options(synchronize_session=False)
    )
    await session.commit()

    # The reading of a deleted sentence is a model call whose answer has
    # nowhere to land, so it is stopped — the same job the Stop button stops,
    # and the only job this endpoint touches.
    if reading:
        from src.sync.queue import mark_cancelled, request_cancel
        for job_id in reading:
            try:
                if not request_cancel(job_id):
                    mark_cancelled(job_id, "the chat it belonged to was deleted")
            except Exception:  # noqa: BLE001
                # The rows are already gone and the caller was answered. A
                # reading left running writes to a row that no longer exists,
                # which is a no-op, not a failure worth a 500.
                logger.exception("automation_reading_not_stopped job=%s", job_id)

    logger.info("automation_session_deleted session=%s runs=%s ws=%s by=%s",
                session_id, len(rows), workspace_id, user.email)


@router.get("/history", response_model=list[RunOut])
async def history(
    limit: int = 20,
    session_id: str | None = None,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    session: AsyncSession = Depends(get_async_session),
) -> list[RunOut]:
    """What this workspace has asked for, newest first.

    Workspace-scoped rather than per-person on purpose: a sweep queued over
    twenty repositories is the workspace's business, and the second person to
    open the page should see that it is already running rather than start it
    again.
    """
    from sqlalchemy import func, select

    query = select(AutomationRun).where(AutomationRun.workspace_id == workspace_id)
    if session_id:
        # The day-buckets the sessions list invents for pre-session rows have
        # to be selectable too, or clicking one of them opens an empty thread.
        query = query.where(func.coalesce(
            AutomationRun.session_id,
            func.concat("day-", func.to_char(
                AutomationRun.created_at, "YYYY-MM-DD")),
        ) == session_id)
    rows = (await session.execute(
        query.order_by(AutomationRun.created_at.desc())
             .limit(max(1, min(limit, 100)))
    )).scalars().all()
    return [_as_run(r) for r in rows]


__all__ = ["router"]
