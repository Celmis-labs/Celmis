"""Project-scoped MCP tokens — issue, list, revoke (superadmin only).

A token from here can search ONE project through the MCP endpoint and do
nothing else (see :mod:`src.mcp_server.project_tokens`). The value is returned
once, in the response to the POST; afterwards only its hash exists.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import client_ip, current_workspace_id, require_superadmin
from src.api.routers.projects import _owned_project
from src.db.session import get_async_session
from src.mcp_server import project_tokens as pt
from src.security.audit import record_action
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectTokenCreate(BaseModel):
    label: str = Field(default="", max_length=pt.MAX_LABEL)
    #: Lifetime in seconds: 1 hour … 90 days (default 30 days).
    ttl_seconds: int | None = Field(default=None)


class ProjectTokenOut(BaseModel):
    id: str
    project_id: str
    label: str
    scopes: list[str]
    created_by: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    #: active | expired | revoked
    status: str


class ProjectTokenCreated(ProjectTokenOut):
    #: The token itself — shown this once.
    token: str


def _out(view: pt.ProjectTokenView) -> dict:
    problem = view.problem()
    state = "active" if not problem else ("revoked" if view.revoked_at else "expired")
    return {
        "id": view.id, "project_id": view.project_id, "label": view.label,
        "scopes": list(view.scopes), "created_by": view.created_by,
        "created_at": view.created_at, "expires_at": view.expires_at,
        "revoked_at": view.revoked_at, "last_used_at": view.last_used_at,
        "status": state,
    }


@router.get("/{project_id}/mcp-tokens", response_model=list[ProjectTokenOut])
async def list_project_tokens(
    project_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_superadmin),
    ws_id: str = Depends(current_workspace_id),
) -> list[dict]:
    await _owned_project(session, project_id, ws_id)
    views = await session.run_sync(lambda s: pt.list_rows(s, project_id))
    return [_out(v) for v in views]


@router.post(
    "/{project_id}/mcp-tokens", response_model=ProjectTokenCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_token(
    project_id: str,
    payload: ProjectTokenCreate,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_superadmin),
    ws_id: str = Depends(current_workspace_id),
) -> dict:
    await _owned_project(session, project_id, ws_id)
    try:
        pt.clamp_ttl(payload.ttl_seconds)
    except pt.ProjectTokenError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    token, view = await session.run_sync(lambda s: pt.mint(
        s, workspace_id=ws_id, project_id=project_id, label=payload.label,
        ttl_seconds=payload.ttl_seconds, created_by=user.email))
    await session.commit()
    record_action(
        action="mcp.project_token.created", actor=user.email, actor_id=user.id,
        workspace_id=ws_id, target=project_id, ip=client_ip(request),
        detail={"token_id": view.id, "expires_at": view.expires_at.isoformat(),
                "scopes": list(view.scopes)},
    )
    logger.info("mcp_project_token_created project=%s token=%s by=%s",
                project_id, view.id, user.id)
    return {**_out(view), "token": token}


@router.delete("/{project_id}/mcp-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_project_token(
    project_id: str,
    token_id: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_superadmin),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    await _owned_project(session, project_id, ws_id)
    ok = await session.run_sync(lambda s: pt.revoke(s, project_id, token_id))
    if not ok:
        raise HTTPException(status_code=404, detail="token not found")
    await session.commit()
    record_action(
        action="mcp.project_token.revoked", actor=user.email, actor_id=user.id,
        workspace_id=ws_id, target=project_id, ip=client_ip(request),
        detail={"token_id": token_id},
    )
    logger.info("mcp_project_token_revoked project=%s token=%s by=%s",
                project_id, token_id, user.id)
