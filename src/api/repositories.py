"""Data-access (repository) layer for Project / Chat / Message.

Encapsulates the SQLAlchemy queries — so that routers stay focused on
HTTP/validation while the raw ORM operations are kept here. Reused in the CLI
migration scripts too.

Pattern: stateless async functions that take an `AsyncSession` + params.
No classes — easier to test, explicit dependencies.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import Chat, Message, Project, ProjectRepo

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# Projects
# ════════════════════════════════════════════════════════════════════


async def list_projects(session: AsyncSession) -> list[Project]:
    """All projects (shared mode — no filter)."""
    stmt = (
        select(Project)
        .options(selectinload(Project.repos))
        .order_by(Project.updated_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_project(
    session: AsyncSession,
    project_id: str,
    *,
    workspace_id: str | None = None,
) -> Project | None:
    """Pass `workspace_id` unless the row is already authorized — see
    `get_chat` for why this argument exists."""
    stmt = (
        select(Project)
        .where(Project.id == project_id)
        .options(selectinload(Project.repos))
    )
    if workspace_id is not None:
        stmt = stmt.where(Project.workspace_id == workspace_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def count_chats_in_project(session: AsyncSession, project_id: str) -> int:
    stmt = select(func.count(Chat.id)).where(Chat.project_id == project_id)
    return int((await session.execute(stmt)).scalar() or 0)


async def create_project(
    session: AsyncSession,
    *,
    name: str,
    description: str | None,
    repos: Iterable[tuple[str, str | None]] = (),
    owner_user_id: str | None = None,
    workspace_id: str = "default",
) -> Project:
    project = Project(
        name=name,
        description=description,
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
    )
    project.repos = [
        ProjectRepo(repo_slug=slug, role=role) for slug, role in repos
    ]
    session.add(project)
    try:
        await session.flush()
    except IntegrityError as e:
        await session.rollback()
        # The DRIVER'S message is not the user's. `{e.orig}` put the asyncpg
        # exception class, the constraint name, the column names and the
        # owner's internal id into a 409 the browser renders verbatim:
        #
        #   project name conflict: <class 'asyncpg.exceptions.
        #   UniqueViolationError'>: duplicate key value violates unique
        #   constraint "uq_projects_name_owner"
        #   DETAIL: Key (name, owner_user_id)=(E2E Probe, 1d8d41e3-…)
        #
        # There is exactly one constraint a caller can violate here and one
        # thing they can do about it, so the sentence says that. The original
        # travels to the log through `from e`, where an operator can read it.
        logger.info("project_name_conflict name=%r owner=%s", name, owner_user_id)
        raise ValueError(
            f"A project named {name!r} already exists. Pick another name."
        ) from e
    await session.refresh(project, attribute_names=["repos"])
    return project


async def update_project(
    session: AsyncSession,
    project_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Project | None:
    project = await get_project(session, project_id)
    if project is None:
        return None
    if name is not None:
        project.name = name
    if description is not None:
        project.description = description
    project.updated_at = datetime.now(UTC)
    await session.flush()
    return project


async def delete_project(session: AsyncSession, project_id: str) -> bool:
    result = await session.execute(delete(Project).where(Project.id == project_id))
    return (result.rowcount or 0) > 0


# ─── ProjectRepo ─────────────────────────────────────────────────────


async def add_repo_to_project(
    session: AsyncSession,
    project_id: str,
    repo_slug: str,
    role: str | None = None,
) -> ProjectRepo | None:
    project = await get_project(session, project_id)
    if project is None:
        return None
    # Idempotent — if it is already there, we update the role
    for r in project.repos:
        if r.repo_slug == repo_slug:
            r.role = role
            await session.flush()
            return r
    link = ProjectRepo(project_id=project_id, repo_slug=repo_slug, role=role)
    session.add(link)
    await session.flush()
    return link


async def remove_repo_from_project(
    session: AsyncSession,
    project_id: str,
    repo_slug: str,
) -> bool:
    result = await session.execute(
        delete(ProjectRepo).where(
            ProjectRepo.project_id == project_id,
            ProjectRepo.repo_slug == repo_slug,
        )
    )
    return (result.rowcount or 0) > 0


# ════════════════════════════════════════════════════════════════════
# Chats
# ════════════════════════════════════════════════════════════════════


async def list_chats(
    session: AsyncSession,
    *,
    project_id: str | None = None,
    repo_slug: str | None = None,
    limit: int = 100,
    workspace_id: str | None = None,
) -> list[Chat]:
    """List of chats. If both filters are None — all of them (for admin/debug)."""
    stmt = select(Chat).order_by(Chat.updated_at.desc()).limit(limit)
    if project_id:
        stmt = stmt.where(Chat.project_id == project_id)
    if repo_slug:
        stmt = stmt.where(Chat.repo_slug == repo_slug)
    if workspace_id:
        stmt = stmt.where(Chat.workspace_id == workspace_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_chat(
    session: AsyncSession,
    chat_id: str,
    *,
    with_messages: bool = True,
    workspace_id: str | None = None,
) -> Chat | None:
    """Pass `workspace_id` unless you have already authorized the row.

    Optional rather than required only because the callers that already run
    `_owned_chat` would then check twice. Every by-id caller that does NOT is
    a cross-tenant read: the ask endpoint loaded a chat straight from its id,
    fed the whole transcript to the model as history, and appended to it.
    """
    stmt = select(Chat).where(Chat.id == chat_id)
    if workspace_id is not None:
        stmt = stmt.where(Chat.workspace_id == workspace_id)
    if with_messages:
        stmt = stmt.options(selectinload(Chat.messages))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_chat(
    session: AsyncSession,
    *,
    project_id: str | None,
    repo_slug: str | None,
    name: str | None = None,
    owner_user_id: str | None = None,
    workspace_id: str = "default",
) -> Chat:
    if project_id is None and repo_slug is None:
        raise ValueError("chat needs either project_id or repo_slug")
    chat = Chat(
        project_id=project_id,
        repo_slug=repo_slug,
        name=name,
        owner_user_id=owner_user_id,
        workspace_id=workspace_id,
    )
    session.add(chat)
    await session.flush()
    return chat


async def delete_chat(session: AsyncSession, chat_id: str) -> bool:
    result = await session.execute(delete(Chat).where(Chat.id == chat_id))
    return (result.rowcount or 0) > 0


async def clear_chat_messages(session: AsyncSession, chat_id: str) -> int:
    """Delete every message — keep the chat itself."""
    result = await session.execute(
        delete(Message).where(Message.chat_id == chat_id)
    )
    return result.rowcount or 0


# ─── Messages ────────────────────────────────────────────────────────


async def append_message(
    session: AsyncSession,
    chat_id: str,
    *,
    role: str,
    content: str,
    meta: dict | None = None,
) -> Message:
    msg = Message(
        chat_id=chat_id,
        role=role,
        content=content,
        meta=meta,
    )
    session.add(msg)
    # Trigger chat.updated_at via ORM onupdate (func.now() server-side).
    # We do this via SELECT+SET because merely INSERTing a message does not
    # trigger onupdate on the parent table.
    chat_obj = await session.get(Chat, chat_id)
    if chat_obj is not None:
        # Changing any tracked attribute triggers onupdate
        chat_obj.name = chat_obj.name  # no-op but SA will see it as dirty
        session.add(chat_obj)
    await session.flush()
    return msg


async def get_messages(
    session: AsyncSession,
    chat_id: str,
    *,
    limit: int | None = None,
) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.chat_id == chat_id)
        .order_by(Message.id)
    )
    if limit:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def auto_name_chat(
    session: AsyncSession,
    chat_id: str,
    user_content: str,
    max_len: int = 80,
) -> None:
    """Update the chat name from the first user message (if name=NULL)."""
    chat = await get_chat(session, chat_id, with_messages=False)
    if chat is None or chat.name:
        return
    snippet = " ".join(user_content.split())[:max_len].strip()
    chat.name = snippet
    await session.flush()


# ════════════════════════════════════════════════════════════════════
# Helpers — repos that contain a specific slug (for membership checks)
# ════════════════════════════════════════════════════════════════════


async def projects_containing_repo(
    session: AsyncSession,
    repo_slug: str,
) -> list[str]:
    """Returns the ids of projects that include the given repo."""
    stmt = select(ProjectRepo.project_id).where(ProjectRepo.repo_slug == repo_slug)
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]
