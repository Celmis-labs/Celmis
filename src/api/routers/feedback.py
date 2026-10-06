"""Review-finding feedback (Stage 23).

Closes the review loop: agents post findings, humans mark which were useful and
which were noise, and the aggregate tells you *which agent* is producing the
noise — the input you need to tune prompts or drop a rule.

    GET    /api/feedback/run/{run_id}     — verdicts for one run
    PUT    /api/feedback/run/{run_id}     — record accept/dismiss for a finding
    DELETE /api/feedback/run/{run_id}/{finding_key}
    GET    /api/feedback/stats            — dismissal rate by agent / severity
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import deps as deps_module
from src.api.deps import current_workspace_id, get_current_user
from src.db.models import FindingFeedback, FindingSignal
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

_VALID_STATES = {"accepted", "dismissed"}
# Preset reasons keep the aggregate meaningful; free text is still allowed.
PRESET_REASONS = ["false_positive", "wont_fix", "not_relevant", "style_nit", "duplicate"]


def finding_key(file_path: str, line: int, title: str, rule_id: str | None = None) -> str:
    """Stable identity for a finding across re-runs.

    Findings are stored as a JSON blob and get reordered between runs, so an
    index is useless as an identifier. Hash the things that actually pin a
    finding down instead.
    """
    from src.review.learning.signals import feedback_key

    return feedback_key(file_path, line, title, rule_id)


def _issue_scope(
    user: User, ws: str, run_id: str,
) -> tuple[bool, tuple[str, str, int] | None]:
    """(may this user change the run's issue?, the run's PR or None).

    The feedback itself is anyone's opinion, but carrying it onto the PR's
    issue CHANGES the issue — the thing PATCH /api/issues allows from member
    up only. A viewer's verdict is recorded and stops there.

    The PR comes from the run row, so the issue is found whichever of the
    PR's runs the feedback was given on; a run of another workspace yields
    none.
    """
    if not user.is_admin:
        role = deps_module.workspace_role(user.id, ws)
        if role not in deps_module.ISSUE_WRITE_ROLES:
            return False, None
    try:
        from src.api.review_runs import get_review_run_store

        found = get_review_run_store().pr_of(run_id)
    except Exception as exc:  # noqa: BLE001 — the run-id match still works
        logger.warning("feedback_run_pr_lookup_failed run=%s err=%s", run_id, exc)
        found = None
    if found is None or found[0] != ws:
        return True, None
    return True, found[1:]


async def _readable_run(run_id: str, user: User, ws: str) -> None:
    """A verdict on a finding is about a run, and a run is the content of one
    repository: a person who may not read that repository gets the answer for
    a run that does not exist (`reviews._can_see`, the same rule the run's own
    pages apply). A run id nobody stored is not a repository's content, so the
    old behaviour stands for it."""
    import asyncio

    from src.api.review_runs import get_review_run_store
    from src.api.routers.reviews import _can_see

    run = await asyncio.to_thread(get_review_run_store().get, run_id)
    if run is not None and not await _can_see(run, user, ws):
        raise HTTPException(status_code=404, detail="Run not found")


async def _sync_issue(
    user: User, ws: str, run_id: str, *, state: str | None,
    file_path: str, title: str, rule_id: str | None,
) -> None:
    import asyncio

    from src.review.issues import apply_feedback

    allowed, pr = await asyncio.to_thread(_issue_scope, user, ws, run_id)
    if not allowed:
        return
    await asyncio.to_thread(
        apply_feedback, workspace_id=ws, run_id=run_id, state=state,
        file_path=file_path, title=title, rule_id=rule_id, pr=pr,
    )


async def _teach(
    user: User, ws: str, run_id: str, *, state: str | None, finding_key_value: str,
    payload: FeedbackIn | None, file_path: str | None = None, title: str | None = None,
    rule_id: str | None = None, reason: str = "",
) -> None:
    """Carry a page verdict onto the learning signals (`state=None` takes it
    back). Only people who may change the run's issue teach; the finding is
    recomputed from the run, the page's own fields are the fallback. Best
    effort: it never fails the request."""
    import asyncio

    try:
        allowed, _pr = await asyncio.to_thread(_issue_scope, user, ws, run_id)
        if not allowed:
            return
        from src.review.learning import signals

        def work() -> None:
            snap = signals.snapshot_from_run(
                run_id, finding_key_value,
                file_path=payload.file_path if payload else file_path,
                title=payload.title if payload else title,
                rule_id=payload.rule_id if payload else rule_id)
            if snap is None and (payload is not None or title is not None):
                snap = signals.FindingSnapshot(
                    title=(payload.title if payload else title) or "",
                    file_path=(payload.file_path if payload else file_path) or "",
                    rule_id=(payload.rule_id if payload else rule_id) or None,
                    agent=payload.agent if payload else None,
                    severity=payload.severity if payload else None)
            if snap is None:
                return
            if state is None:
                signals.clear_ui(ws, run_id, actor=user.email or user.id, snapshot=snap)
                return
            signals.record_ui(
                ws, run_id, state, actor=user.email or user.id, also_known_as=[user.id],
                actor_is_member=True, reason=reason, snapshot=snap)

        await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001
        logger.warning("feedback_learning_failed run=%s err_type=%s", run_id,
                       type(exc).__name__)


class FeedbackIn(BaseModel):
    finding_key: str = Field(min_length=4, max_length=64)
    state: str
    reason: str = Field(default="", max_length=500)
    agent: str | None = None
    severity: str | None = None
    repo_slug: str | None = None
    # What identifies the finding across runs. Optional, and only used to
    # carry a dismissal onto the PR's issue (src/review/issues.py): the
    # `finding_key` is line-sensitive and client-minted, so it cannot.
    file_path: str | None = Field(default=None, max_length=1000)
    title: str | None = Field(default=None, max_length=2000)
    rule_id: str | None = Field(default=None, max_length=300)


class FeedbackOut(BaseModel):
    finding_key: str
    state: str
    reason: str
    agent: str | None
    severity: str | None
    user_id: str | None


class AgentStat(BaseModel):
    agent: str
    accepted: int
    dismissed: int
    dismissal_rate_pct: float
    #: Findings later fixed in the code (the issues ledger, via the learning
    #: signals) — what the team DID, next to what it clicked.
    implemented: int = 0


@router.get("/run/{run_id}", response_model=list[FeedbackOut])
async def list_for_run(
    run_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> list[FeedbackOut]:
    await _readable_run(run_id, user, ws)
    rows = (await session.scalars(
        select(FindingFeedback).where(
            FindingFeedback.run_id == run_id,
            FindingFeedback.workspace_id == ws,
        )
    )).all()
    return [
        FeedbackOut(
            finding_key=r.finding_key, state=r.state, reason=r.reason,
            agent=r.agent, severity=r.severity, user_id=r.user_id,
        )
        for r in rows
    ]


@router.put("/run/{run_id}", response_model=FeedbackOut)
async def upsert_feedback(
    run_id: str,
    payload: FeedbackIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> FeedbackOut:
    if payload.state not in _VALID_STATES:
        raise HTTPException(
            status_code=422, detail=f"state must be one of {sorted(_VALID_STATES)}",
        )
    await _readable_run(run_id, user, ws)
    row = (await session.scalars(
        select(FindingFeedback).where(
            FindingFeedback.workspace_id == ws,
            FindingFeedback.run_id == run_id,
            FindingFeedback.finding_key == payload.finding_key,
        )
    )).first()
    if row is None:
        row = FindingFeedback(
            id=str(uuid.uuid4()), workspace_id=ws, run_id=run_id,
            finding_key=payload.finding_key,
        )
        session.add(row)
    row.state = payload.state
    row.reason = payload.reason
    row.agent = payload.agent
    row.severity = payload.severity
    row.repo_slug = payload.repo_slug
    row.user_id = user.id
    await session.commit()
    logger.info(
        "finding_feedback run=%s key=%s state=%s agent=%s by=%s",
        run_id, payload.finding_key, payload.state, payload.agent, user.email,
    )
    if payload.file_path and payload.title is not None:
        await _sync_issue(
            user, ws, run_id, state=payload.state,
            file_path=payload.file_path, title=payload.title,
            rule_id=payload.rule_id,
        )
    await _teach(user, ws, run_id, state=payload.state,
                 finding_key_value=payload.finding_key, payload=payload,
                 reason=payload.reason)
    return FeedbackOut(
        finding_key=row.finding_key, state=row.state, reason=row.reason,
        agent=row.agent, severity=row.severity, user_id=row.user_id,
    )


@router.delete("/run/{run_id}/{fkey}", status_code=204)
async def clear_feedback(
    run_id: str, fkey: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
    file_path: str | None = None,
    title: str | None = None,
    rule_id: str | None = None,
) -> None:
    await _readable_run(run_id, user, ws)
    row = (await session.scalars(
        select(FindingFeedback).where(
            FindingFeedback.workspace_id == ws,
            FindingFeedback.run_id == run_id,
            FindingFeedback.finding_key == fkey,
        )
    )).first()
    if row is not None:
        await session.delete(row)
        await session.commit()
    if file_path and title is not None:
        # Undoing a dismissal reopens the issue it dismissed — and only that:
        # `apply_feedback` leaves a status somebody set elsewhere alone.
        await _sync_issue(
            user, ws, run_id, state=None,
            file_path=file_path, title=title, rule_id=rule_id,
        )
    await _teach(user, ws, run_id, state=None, finding_key_value=fkey, payload=None,
                 file_path=file_path, title=title, rule_id=rule_id)


@router.get("/stats", response_model=list[AgentStat])
async def stats(
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> list[AgentStat]:
    """Dismissal rate per agent — the signal for which agent needs tuning."""
    rows = (await session.execute(
        select(
            FindingFeedback.agent,
            func.count().filter(FindingFeedback.state == "accepted"),
            func.count().filter(FindingFeedback.state == "dismissed"),
        )
        .where(FindingFeedback.workspace_id == ws)
        .group_by(FindingFeedback.agent)
    )).all()
    fixed = dict((await session.execute(
        select(FindingSignal.agent, func.count())
        .where(FindingSignal.workspace_id == ws, FindingSignal.signal == "implemented")
        .group_by(FindingSignal.agent)
    )).all())
    out: list[AgentStat] = []
    seen: set[str | None] = set()
    for agent, accepted, dismissed in rows:
        seen.add(agent)
        total = int(accepted) + int(dismissed)
        out.append(AgentStat(
            agent=agent or "—",
            accepted=int(accepted), dismissed=int(dismissed),
            dismissal_rate_pct=round(int(dismissed) / total * 100, 1) if total else 0.0,
            implemented=int(fixed.get(agent, 0)),
        ))
    for agent, n in fixed.items():
        if agent not in seen:
            out.append(AgentStat(agent=agent or "—", accepted=0, dismissed=0,
                                 dismissal_rate_pct=0.0, implemented=int(n)))
    return sorted(out, key=lambda a: -a.dismissal_rate_pct)
