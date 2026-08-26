"""Compliance checks CRUD.

Endpoints:
    GET    /api/compliance                — list all
    POST   /api/compliance                — create
    PUT    /api/compliance/{id}           — replace
    DELETE /api/compliance/{id}           — remove

Every mutating endpoint is admin-only (matches /admin/agents pattern).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user, require_admin
from src.db.models import ComplianceCheck
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/compliance", tags=["compliance"])


class ComplianceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    scope: str = Field(default="workspace", max_length=200)
    glob_pattern: str = Field(default="**", max_length=200)
    rule: str = Field(min_length=1, max_length=8000)
    severity: str = Field(default="error", pattern="^(error|warn)$")
    blocking: bool = True
    enabled: bool = True

    model_config = ConfigDict(extra="forbid")


class ComplianceOut(BaseModel):
    id: str
    name: str
    description: str
    scope: str
    glob_pattern: str
    rule: str
    severity: str
    blocking: bool
    enabled: bool
    created_by: str | None
    model_config = ConfigDict(from_attributes=True)


def _out(r: ComplianceCheck) -> ComplianceOut:
    return ComplianceOut(
        id=r.id, name=r.name, description=r.description, scope=r.scope,
        glob_pattern=r.glob_pattern, rule=r.rule, severity=r.severity,
        blocking=r.blocking, enabled=r.enabled, created_by=r.created_by,
    )


@router.get("", response_model=list[ComplianceOut])
async def list_checks(
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[ComplianceOut]:
    rows = (await session.scalars(
        select(ComplianceCheck)
        .where(ComplianceCheck.workspace_id == ws_id)
        .order_by(ComplianceCheck.scope, ComplianceCheck.name)
    )).all()
    return [_out(r) for r in rows]


@router.post("", response_model=ComplianceOut, status_code=201)
async def create_check(
    payload: ComplianceIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_admin),
    ws_id: str = Depends(current_workspace_id),
) -> ComplianceOut:
    row = ComplianceCheck(
        id=str(uuid.uuid4()),
        workspace_id=ws_id,
        name=payload.name, description=payload.description,
        scope=payload.scope, glob_pattern=payload.glob_pattern,
        rule=payload.rule, severity=payload.severity,
        blocking=payload.blocking, enabled=payload.enabled,
        created_by=user.email,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info("compliance_created id=%s scope=%s by=%s", row.id, row.scope, user.email)
    return _out(row)


@router.put("/{check_id}", response_model=ComplianceOut)
async def replace_check(
    check_id: str,
    payload: ComplianceIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_admin),
) -> ComplianceOut:
    row = await session.get(ComplianceCheck, check_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    row.name = payload.name
    row.description = payload.description
    row.scope = payload.scope
    row.glob_pattern = payload.glob_pattern
    row.rule = payload.rule
    row.severity = payload.severity
    row.blocking = payload.blocking
    row.enabled = payload.enabled
    await session.commit()
    await session.refresh(row)
    logger.info("compliance_updated id=%s by=%s", check_id, user.email)
    return _out(row)


@router.delete("/{check_id}", status_code=204)
async def delete_check(
    check_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_admin),
) -> None:
    row = await session.get(ComplianceCheck, check_id)
    if row is None:
        return
    await session.delete(row)
    await session.commit()
    logger.info("compliance_deleted id=%s by=%s", check_id, user.email)


__all__ = ["router"]
