"""Web-push subscription management.

    GET    /api/push/config      — is push available, and the VAPID public key
    POST   /api/push/subscribe   — register (or refresh) this browser
    DELETE /api/push/subscribe   — drop this browser
    POST   /api/push/test        — send yourself one, to prove the chain works

The test endpoint earns its place: between a service worker, a permission
grant, a VAPID pair and a push service, a silent failure has four plausible
causes. One button that either produces a notification or an error is the
difference between debugging and guessing.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.db.models import PushSubscription
from src.db.session import get_async_session
from src.notifications import webpush
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/push", tags=["push"])


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=400)
    auth: str = Field(min_length=1, max_length=400)
    model_config = ConfigDict(extra="ignore")


class SubscribeIn(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2000)
    keys: SubscriptionKeys
    # The browser's own PushSubscription JSON carries more than we need;
    # ignoring the rest keeps a spec change from 422-ing every client.
    model_config = ConfigDict(extra="ignore")


class UnsubscribeIn(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2000)
    model_config = ConfigDict(extra="forbid")


@router.get("/config")
async def push_config(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    """What the client needs before it can even ask for permission."""
    state = webpush.status()
    devices = 0
    if state["enabled"]:
        rows = (await session.scalars(
            select(PushSubscription).where(PushSubscription.user_id == user.id)
        )).all()
        devices = len(rows)
    return {"enabled": state["enabled"], "public_key": state["public_key"],
            "devices": devices}


@router.post("/subscribe", status_code=201)
async def subscribe(
    payload: SubscribeIn,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
    workspace_id: str = Depends(current_workspace_id),
) -> dict:
    if not webpush.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Push notifications are not configured on this server.",
        )

    # The endpoint identifies the browser install. Re-subscribing (keys rotate,
    # or the service worker re-registers) must replace the row — otherwise the
    # same device collects duplicates and gets one copy of every notification
    # per stale row.
    existing = (await session.scalars(
        select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint)
    )).first()
    ua = (request.headers.get("user-agent") or "")[:300]
    if existing is not None:
        existing.user_id = user.id
        existing.workspace_id = workspace_id
        existing.p256dh = payload.keys.p256dh
        existing.auth = payload.keys.auth
        existing.user_agent = ua
        existing.failure_count = 0
    else:
        session.add(PushSubscription(
            id=str(uuid.uuid4()),
            user_id=user.id,
            workspace_id=workspace_id,
            endpoint=payload.endpoint,
            p256dh=payload.keys.p256dh,
            auth=payload.keys.auth,
            user_agent=ua,
        ))
    await session.commit()
    logger.info("push_subscribed user=%s ws=%s new=%s",
                user.email, workspace_id, existing is None)
    return {"ok": True}


@router.delete("/subscribe")
async def unsubscribe(
    payload: UnsubscribeIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    # Scoped to the caller: an endpoint string is not a capability to delete
    # someone else's row.
    await session.execute(
        delete(PushSubscription).where(
            PushSubscription.endpoint == payload.endpoint,
            PushSubscription.user_id == user.id,
        )
    )
    await session.commit()
    return {"ok": True}


@router.post("/test")
async def send_test(
    user: User = Depends(get_current_user),
) -> dict:
    import asyncio

    if not webpush.is_enabled():
        raise HTTPException(status_code=503, detail="Push is not configured.")
    counts = await asyncio.to_thread(
        webpush.send_to_user,
        user.id,
        title="Celmis",
        body="Notifications are working. This is what a finished session looks like.",
        url="/claude",
        tag="celmis-test",
    )
    if not counts["sent"]:
        raise HTTPException(
            status_code=409,
            detail=("No device accepted the notification — "
                    f"expired: {counts['expired']}, failed: {counts['failed']}. "
                    "Re-enable notifications on this device."),
        )
    return counts


__all__ = ["router"]
