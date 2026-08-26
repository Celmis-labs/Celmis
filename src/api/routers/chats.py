"""Chat routes — CRUD chats + messages.

Q&A streaming (ask) — separately in `qa.py` (a different router, because of
the SSE response). Here — simple CRUD without LLM calls.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import repositories as repo
from src.api.deps import current_workspace_id, get_current_user
from src.api.schemas import ChatIn, ChatOut, MessageOut
from src.db.models import Chat
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

async def _owned_chat(session: AsyncSession, chat_id: str, ws_id: str) -> Chat:
    """The chat, or 404 — never another tenant's conversation.

    A chat holds the questions people asked about their own source and the
    answers quoting it. The by-id handlers loaded straight from the id while
    the list endpoint filtered by workspace, so the whole transcript was
    readable, and deletable, by anyone who knew the id.
    """
    chat = await session.get(Chat, chat_id)
    if chat is None or chat.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="chat not found")
    return chat



router = APIRouter(prefix="/api/chats", tags=["chats"])


def _chat_to_out(chat, *, include_messages: bool = False) -> ChatOut:
    out = ChatOut(
        id=chat.id,
        project_id=chat.project_id,
        repo_slug=chat.repo_slug,
        name=chat.name,
        owner_user_id=chat.owner_user_id,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        messages_count=len(chat.messages or []) if include_messages else 0,
    )
    if include_messages and chat.messages:
        out.messages = [
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                timestamp=m.timestamp,
                meta=m.meta,
            )
            for m in chat.messages
        ]
        out.messages_count = len(out.messages)
    return out


@router.get("", response_model=list[ChatOut])
async def list_chats(
    project_id: str | None = Query(default=None),
    repo_slug: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[ChatOut]:
    """List chats scoped to the caller's active workspace.
    Messages are not loaded (lazy='noload') — a separate GET /chats/{id}."""
    chats = await repo.list_chats(
        session,
        project_id=project_id,
        repo_slug=repo_slug,
        limit=limit,
        workspace_id=ws_id,
    )
    out = []
    for c in chats:
        # messages_count via a separate count (because lazy='noload')
        # Simpler that way — a separate query, not a bug
        from sqlalchemy import func, select

        from src.db.models import Message
        n = (
            await session.execute(
                select(func.count(Message.id)).where(Message.chat_id == c.id)
            )
        ).scalar() or 0
        co = _chat_to_out(c, include_messages=False)
        co.messages_count = int(n)
        out.append(co)
    return out


@router.post("", response_model=ChatOut, status_code=status.HTTP_201_CREATED)
async def create_chat(
    payload: ChatIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> ChatOut:
    # The chat row carries this workspace, but its TARGETS came from the
    # request body and were never checked. A chat created in your own
    # workspace naming another tenant's project resolves to that tenant's repo
    # list when the ask endpoint dereferences it; naming their repo slug
    # points retrieval straight at their source. Both are validated here, at
    # the only place a chat's targets are ever set.
    if payload.project_id:
        from src.api.routers.projects import _owned_project
        await _owned_project(session, payload.project_id, ws_id)
    if payload.repo_slug:
        from src.api.auto_review import get_auto_review_store
        if get_auto_review_store().get_in_workspace(ws_id, payload.repo_slug) is None:
            raise HTTPException(status_code=404, detail="repo not registered")

    try:
        chat = await repo.create_chat(
            session,
            project_id=payload.project_id,
            repo_slug=payload.repo_slug,
            name=payload.name,
            owner_user_id=user.id,
            workspace_id=ws_id,
        )
        await session.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info(
        "chat_created id=%s project=%s repo=%s by=%s",
        chat.id, chat.project_id, chat.repo_slug, user.id,
    )
    return _chat_to_out(chat, include_messages=False)


@router.get("/{chat_id}", response_model=ChatOut)
async def get_chat(
    chat_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> ChatOut:
    await _owned_chat(session, chat_id, ws_id)
    chat = await repo.get_chat(session, chat_id, with_messages=True)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    return _chat_to_out(chat, include_messages=True)


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    chat_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    await _owned_chat(session, chat_id, ws_id)
    ok = await repo.delete_chat(session, chat_id)
    if not ok:
        raise HTTPException(status_code=404, detail="chat not found")
    await session.commit()


@router.post("/{chat_id}/clear", status_code=status.HTTP_200_OK)
async def clear_messages(
    chat_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> dict:
    await _owned_chat(session, chat_id, ws_id)
    chat = await repo.get_chat(session, chat_id, with_messages=False)
    if chat is None:
        raise HTTPException(status_code=404, detail="chat not found")
    n = await repo.clear_chat_messages(session, chat_id)
    await session.commit()
    return {"deleted": n}
