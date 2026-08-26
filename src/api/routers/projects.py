"""Project routes — project CRUD + management of repo membership.

Project = a logical group of repos for multi-repo Q&A. All users see all
projects (shared mode), but owner_user_id is stored for audit.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import repositories as repo
from src.api.deps import current_workspace_id, get_current_user
from src.api.schemas import (
    ProjectIn,
    ProjectOut,
    ProjectRepoIn,
    ProjectRepoOut,
)
from src.db.models import Project
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

async def _owned_project(session: AsyncSession, project_id: str, ws_id: str) -> Project:
    """The project, or 404 — never another tenant's row.

    Every by-id handler here used to load straight from the id, while the list
    endpoint eight lines up filtered by workspace. So a project was private
    until someone knew its id, at which point any signed-in user of any
    workspace could read it, rename its repo set, or DELETE it.

    404 rather than 403: whether a project exists in another workspace is not
    this caller's business, and a 403 answers that question.
    """
    project = await session.get(Project, project_id)
    if project is None or project.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="project not found")
    return project



router = APIRouter(prefix="/api/projects", tags=["projects"])


def _to_out(project, chats_count: int = 0) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        owner_user_id=project.owner_user_id,
        created_at=project.created_at,
        updated_at=project.updated_at,
        repos=[
            ProjectRepoOut(
                repo_slug=r.repo_slug,
                role=r.role,
                added_at=r.added_at,
            )
            for r in (project.repos or [])
        ],
        chats_count=chats_count,
    )


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[ProjectOut]:
    """List projects scoped to caller's active workspace."""
    # Prefer scoped list; fall back to unscoped if the helper doesn't
    # accept a filter (keeps backwards compat with legacy helper API).
    projects = (await session.scalars(
        select(Project).where(Project.workspace_id == ws_id)
        .order_by(Project.updated_at.desc())
    )).all()
    out = []
    for p in projects:
        c = await repo.count_chats_in_project(session, p.id)
        out.append(_to_out(p, chats_count=c))
    return out


def _require_registered(repo_slugs: list[str], ws_id: str) -> None:
    """Every slug must name a repository registered in this workspace.

    `POST /api/chats` has always checked this and answers 404 "repo not
    registered"; `POST /api/projects` did not, and returned 201 for
    `{"repo_slug": "github_does-not-exist-anywhere"}`. Two endpoints that set
    the same kind of target disagreed about whether the target has to exist.

    The cost is not theoretical: a project is what a question is asked
    against, so a silently bogus member means retrieval quietly searches one
    repository fewer than the user believes it does, and nothing anywhere says
    so.
    """
    from src.api.auto_review import get_auto_review_store

    store = get_auto_review_store()
    missing = [
        slug for slug in repo_slugs
        if store.get_in_workspace(ws_id, slug) is None
    ]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=(
                "repo not registered in this workspace: "
                + ", ".join(sorted(missing))
            ),
        )


@router.post(
    "",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    payload: ProjectIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> ProjectOut:
    _require_registered([r.repo_slug for r in payload.repos], ws_id)
    try:
        project = await repo.create_project(
            session,
            name=payload.name,
            description=payload.description,
            repos=[(r.repo_slug, r.role) for r in payload.repos],
            owner_user_id=user.id,
            workspace_id=ws_id,
        )
        await session.commit()
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        ) from e
    logger.info("project_created id=%s name=%s by=%s", project.id, project.name, user.id)
    return _to_out(project, chats_count=0)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> ProjectOut:
    await _owned_project(session, project_id, ws_id)
    project = await repo.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    chats_count = await repo.count_chats_in_project(session, project_id)
    return _to_out(project, chats_count=chats_count)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    await _owned_project(session, project_id, ws_id)
    ok = await repo.delete_project(session, project_id)
    if not ok:
        raise HTTPException(status_code=404, detail="project not found")
    await session.commit()
    logger.info("project_deleted id=%s by=%s", project_id, user.id)


# ─── Repo membership ─────────────────────────────────────────────────


@router.post(
    "/{project_id}/repos",
    response_model=ProjectRepoOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_repo(
    project_id: str,
    payload: ProjectRepoIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> ProjectRepoOut:
    await _owned_project(session, project_id, ws_id)
    # Same check as creation. Adding a member one at a time is the other way
    # into the same silently-bogus project.
    _require_registered([payload.repo_slug], ws_id)
    link = await repo.add_repo_to_project(
        session,
        project_id,
        repo_slug=payload.repo_slug,
        role=payload.role,
    )
    if link is None:
        raise HTTPException(status_code=404, detail="project not found")
    await session.commit()
    # A PROJECT IS A GROUP NOW, so its membership changing is exactly when its
    # cross-repo edges go stale. Without this they would be rebuilt only at the
    # next index of one of the members — which for a repository nobody pushes
    # to is never, and the review would keep reporting a radius computed
    # before this repository joined.
    #
    # Non-fatal: the link is committed, and a missed rebuild costs freshness,
    # not correctness.
    try:
        from src.groups.indexer import enqueue_materialize_for_repo

        await asyncio.to_thread(
            enqueue_materialize_for_repo,
            payload.repo_slug,
            enqueued_by=f"project:{project_id}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("project_materialize_enqueue_failed project=%s err=%s",
                       project_id, exc)
    return ProjectRepoOut(
        repo_slug=link.repo_slug, role=link.role, added_at=link.added_at
    )


@router.delete(
    "/{project_id}/repos/{repo_slug}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_repo(
    project_id: str,
    repo_slug: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    await _owned_project(session, project_id, ws_id)
    ok = await repo.remove_repo_from_project(session, project_id, repo_slug)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"repo {repo_slug} not linked to project",
        )
    await session.commit()
