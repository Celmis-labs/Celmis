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

import hashlib
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.db.models import FindingFeedback
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
    basis = f"{rule_id or ''}|{file_path}|{line}|{title.strip()[:120]}"
    return hashlib.sha256(basis.encode()).hexdigest()[:20]


class FeedbackIn(BaseModel):
    finding_key: str = Field(min_length=4, max_length=64)
    state: str
    reason: str = Field(default="", max_length=500)
    agent: str | None = None
    severity: str | None = None
    repo_slug: str | None = None


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


@router.get("/run/{run_id}", response_model=list[FeedbackOut])
async def list_for_run(
    run_id: str,
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> list[FeedbackOut]:
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
    row = (await session.scalars(
        select(FindingFeedback).where(
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
    return FeedbackOut(
        finding_key=row.finding_key, state=row.state, reason=row.reason,
        agent=row.agent, severity=row.severity, user_id=row.user_id,
    )


@router.delete("/run/{run_id}/{fkey}", status_code=204)
async def clear_feedback(
    run_id: str, fkey: str,
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
) -> None:
    row = (await session.scalars(
        select(FindingFeedback).where(
            FindingFeedback.run_id == run_id,
            FindingFeedback.finding_key == fkey,
        )
    )).first()
    if row is not None:
        await session.delete(row)
        await session.commit()


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
    out: list[AgentStat] = []
    for agent, accepted, dismissed in rows:
        total = int(accepted) + int(dismissed)
        out.append(AgentStat(
            agent=agent or "—",
            accepted=int(accepted), dismissed=int(dismissed),
            dismissal_rate_pct=round(int(dismissed) / total * 100, 1) if total else 0.0,
        ))
    return sorted(out, key=lambda a: -a.dismissal_rate_pct)
