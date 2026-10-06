"""Team memories — the facts every review is told (/memories).

Endpoints:
    GET    /api/memories                — list (scope/repo/status/origin/q)
    POST   /api/memories                — add one memory
    PATCH  /api/memories/{id}           — edit one memory (text, directory, status)
    DELETE /api/memories/{id}           — delete one memory
    POST   /api/memories/bulk-status    — approve / reject / re-queue many
    POST   /api/memories/bulk-delete    — delete many
    GET    /api/memories/preview        — what a review of some files would be told

Who may do what, by the same rules as the review rules:

    everything  editor, admin or owner of the active workspace (or a global
                admin): viewers and members get 403 on every endpoint here;
    read        a repository's memories only when the caller's teams grant
                `read` on the repository (owners and admins hold every repo of
                their workspace). A repository one may not read is not named,
                not counted and not previewed; the workspace-wide memories are
                always shown;
    write       the repository must be registered in this workspace and the
                caller's teams must grant `review` on it.

Every row is read and written in the ACTIVE workspace only: an id or a slug of
another workspace answers 404, never its data. Every write is audited (the
ids and the shape of the change, never the text).

The reading and writing itself is `src.review.memories` — the module the PR
commands call too, so a memory cannot be valid in one place and refused in the
other.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    current_workspace_id,
    enforce_repo_permission,
    readable_repo_slugs,
    require_memories_access,
)
from src.api.routers.review_rules import _audit, _can_edit, _writable_repo
from src.db.session import get_async_session
from src.review import memories as store
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memories", tags=["memories"])

Status = Literal["active", "pending", "rejected"]


# ─── Schemas ─────────────────────────────────────────────────────────


class MemoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: None = a workspace-wide memory; a slug = that repository's memory.
    repo_slug: str | None = Field(default=None, max_length=300)
    #: Files the memory is about, relative to the repository root. A plain
    #: directory (`src/billing`) means everything under it. Needs a repository.
    path_glob: str | None = Field(default=None, max_length=store.MAX_GLOB)
    text: str = Field(min_length=1, max_length=store.MAX_TEXT)
    status: Literal["active", "pending"] = "active"


class MemoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=store.MAX_TEXT)
    path_glob: str | None = Field(default=None, max_length=store.MAX_GLOB)
    status: Status | None = None


class BulkStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=500)
    status: Status


class BulkDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=500)


# ─── Helpers ─────────────────────────────────────────────────────────


def _store_error(exc: Exception) -> HTTPException:
    if isinstance(exc, store.MemoryValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, store.MemoryConflictError | store.MemoryLimitError):
        return HTTPException(status_code=409, detail=str(exc))
    raise exc


async def _memories_in_ws(
    ids: list[int], ws_id: str, user: User, session: AsyncSession,
) -> list[dict]:
    """The memories among `ids` that belong to this workspace, after checking
    the caller may write to every repository they belong to. 404 when none of
    them is this workspace's, or when one belongs to a repository the caller
    may not even read (it must not be told that it exists)."""
    rows = await store.get_memories(ws_id, ids, session=session)
    if not rows:
        raise HTTPException(status_code=404, detail="No such memory in this workspace")
    slugs = sorted({r["repo_slug"] for r in rows if r["repo_slug"]})
    seen = await readable_repo_slugs(user, ws_id, slugs)
    if any(slug not in seen for slug in slugs):
        raise HTTPException(status_code=404, detail="No such memory in this workspace")
    for slug in slugs:
        await enforce_repo_permission(slug, user, "review", ws_id)
    return rows


async def _readable_repo(repo: str | None, user: User, ws_id: str) -> str | None:
    """The repository a read asks about, or None for the workspace scope; 403
    unless the caller may read it."""
    slug = (repo or "").strip() or None
    if slug:
        await enforce_repo_permission(slug, user, "read", ws_id)
    return slug


_SOURCE_FIELDS = ("source_provider", "source_repo", "source_pr", "source_comment_id",
                  "source_url")


async def _hide_unreadable_sources(
    rows: list[dict], user: User, ws_id: str,
) -> list[dict]:
    """`rows` with the source of a memory (repository, pull request, comment
    link) blanked unless the caller may read that repository. A workspace-wide
    memory is shown to everybody, and it was taught in one repository's pull
    request: the rule is theirs to see, the place it came from is not."""
    from src.review.learning.signals import local_slug

    slugs: dict[int, str] = {}
    for row in rows:
        if row.get("source_repo"):
            slugs[row["id"]] = local_slug(str(row.get("source_provider") or ""),
                                          str(row["source_repo"]))
    readable = await readable_repo_slugs(user, ws_id, sorted(set(slugs.values())))
    out = []
    for row in rows:
        slug = slugs.get(row["id"])
        if slug is not None and slug not in readable:
            row = {**row, **dict.fromkeys(_SOURCE_FIELDS)}
        out.append(row)
    return out


def _targets(rows: list[dict]) -> str:
    return ",".join(sorted({r["repo_slug"] or "workspace" for r in rows}))


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("")
async def list_memories(
    scope: Literal["all", "workspace", "repo", "directory"] = Query(default="all"),
    repo: str | None = Query(default=None, max_length=300),
    status_: Status | None = Query(default=None, alias="status"),
    origin: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=200),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The workspace's memories. With `repo`, that repository's own (its
    directory ones included). `counts` follow the same filters except
    `status`, so the tabs can show how many wait for approval. Without `repo`
    the repositories' memories of the ones the caller may not read are left
    out, of the rows and of the counts alike."""
    repo_slug = await _readable_repo(repo, user, ws_id)
    scope_arg = None if scope == "all" else scope
    memories = await store.list_memories(
        ws_id, repo_slug, status_, scope=scope_arg, origin=origin, q=q, session=session)
    everything = await store.list_memories(
        ws_id, repo_slug, scope=scope_arg, origin=origin, q=q, session=session)
    if repo_slug is None:
        shown = await readable_repo_slugs(
            user, ws_id, sorted({r["repo_slug"] for r in everything if r["repo_slug"]}))

        def _visible(row: dict) -> bool:
            return not row["repo_slug"] or row["repo_slug"] in shown

        memories = [r for r in memories if _visible(r)]
        everything = [r for r in everything if _visible(r)]
    counts = {"all": len(everything), "active": 0, "pending": 0, "rejected": 0}
    for row in everything:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    memories = await _hide_unreadable_sources(memories, user, ws_id)
    return {
        "memories": memories,
        "counts": counts,
        "repo_slug": repo_slug,
        "can_edit": await _can_edit(user, ws_id),
        "statuses": list(store.STATUSES),
        "origins": list(store.ORIGINS),
        "scopes": list(store.SCOPES),
        "max_text": store.MAX_TEXT,
        "max_per_scope": store.MAX_MEMORIES_PER_SCOPE,
    }


@router.get("/preview")
async def preview_memories(
    repo: str | None = Query(default=None, max_length=300),
    paths: list[str] | None = Query(default=None, max_length=200),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """What a review of `paths` of `repo` would be told: the active memories
    that apply (a directory memory only when a path is under it), the ones the
    prompt budget keeps, and the characters they take. Without `paths` every
    directory memory of the repository is shown."""
    from src.review.policy_rules import render_memories
    from src.review.settings import get_review_settings

    repo_slug = await _readable_repo(repo, user, ws_id)
    rows = await store.list_memories(
        ws_id, None, "active", scope="workspace", session=session)
    if repo_slug:
        rows += await store.list_memories(ws_id, repo_slug, "active", session=session)
    enabled = await asyncio.to_thread(store.memories_enabled_sync, ws_id, repo_slug)
    budget = int(get_review_settings().memory_prompt_chars)
    given = [p.strip() for p in (paths or []) if p and p.strip()]
    rendered = render_memories(
        rows if enabled else [], given, budget=budget, match_files=bool(given))
    kept = set(rendered.used)
    return {
        "enabled": bool(enabled),
        "budget": budget,
        "chars": len(rendered.text),
        "omitted": rendered.omitted,
        "used": await _hide_unreadable_sources(
            [r for r in rows if r["id"] in kept], user, ws_id),
        "text": rendered.text,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_memory(
    payload: MemoryIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    repo_slug = None
    if payload.repo_slug and payload.repo_slug.strip():
        repo_slug = await _writable_repo(payload.repo_slug, user, ws_id)
    try:
        memory = await store.create_memory(
            ws_id, repo_slug=repo_slug, text=payload.text, path_glob=payload.path_glob,
            status=payload.status, origin="ui", created_by=user.email, session=session)
    except (store.MemoryValidationError, store.MemoryConflictError,
            store.MemoryLimitError) as exc:
        raise _store_error(exc) from exc
    _audit("memories.created", user=user, ws_id=ws_id, target=repo_slug, request=request,
           detail={"ids": [memory["id"]], "status": memory["status"],
                   "scope": memory["scope"]})
    return (await _hide_unreadable_sources([memory], user, ws_id))[0]


@router.patch("/{memory_id}")
async def patch_memory(
    memory_id: int,
    payload: MemoryPatch,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    await _memories_in_ws([memory_id], ws_id, user, session)
    changes = {k: getattr(payload, k) for k in payload.model_fields_set}
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to change")
    try:
        memory = await store.update_memory(
            ws_id, memory_id, changes, user.email, session=session)
    except (store.MemoryValidationError, store.MemoryConflictError,
            store.MemoryLimitError) as exc:
        raise _store_error(exc) from exc
    if memory is None:
        raise HTTPException(status_code=404, detail="No such memory in this workspace")
    _audit("memories.updated", user=user, ws_id=ws_id, target=memory["repo_slug"],
           request=request, detail={"ids": [memory_id], "fields": sorted(changes)})
    return (await _hide_unreadable_sources([memory], user, ws_id))[0]


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: int,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    rows = await _memories_in_ws([memory_id], ws_id, user, session)
    done = await store.delete_memories(ws_id, [r["id"] for r in rows], user.email,
                                       session=session)
    _audit("memories.deleted", user=user, ws_id=ws_id, target=_targets(rows),
           request=request, detail={"ids": done})
    return {"deleted": done}


@router.post("/bulk-status")
async def bulk_status(
    payload: BulkStatusIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Approve (active), reject or send back to pending — many at once."""
    rows = await _memories_in_ws(payload.ids, ws_id, user, session)
    try:
        done = await store.set_status(ws_id, [r["id"] for r in rows], payload.status,
                                      user.email, session=session)
    except (store.MemoryValidationError, store.MemoryLimitError) as exc:
        raise _store_error(exc) from exc
    _audit("memories.status_changed", user=user, ws_id=ws_id, target=_targets(rows),
           request=request, detail={"ids": done, "status": payload.status})
    return {"updated": done, "status": payload.status}


@router.post("/bulk-delete")
async def bulk_delete(
    payload: BulkDeleteIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    rows = await _memories_in_ws(payload.ids, ws_id, user, session)
    done = await store.delete_memories(ws_id, [r["id"] for r in rows], user.email,
                                       session=session)
    _audit("memories.deleted", user=user, ws_id=ws_id, target=_targets(rows),
           request=request, detail={"ids": done})
    return {"deleted": done}
