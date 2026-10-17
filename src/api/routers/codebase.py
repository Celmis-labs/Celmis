"""Codebase — knowledge packs: skills, API examples, checklists and notes.

A pack is a named, versioned bundle of text files that lives in ONE workspace
and is shown to the people its audience and grants name. This router is the
whole REST surface; the rules are in `src/knowledge/` and none are repeated here.

Every route runs three gates in this order, so nothing about a pack is learnt
from which one refused:

1. a NAMED role gate (`require_codebase_reader`, `require_pack_author`,
   `require_signed_in`) — the same 403 for a pack that exists and one that
   does not;
2. the pack is loaded by (workspace, id) through the visibility filter —
   `404 pack not found or not accessible` for a pack that does not exist, is
   another workspace's, is hidden from the caller, or whose id is not an id;
3. only then the operation's permission (`403 Requires a pack maintainer`).

Hidden is missing, byte for byte: lists, counts, search, the brief, versions,
items, archives and the invite preview never name a pack or a team the caller
cannot see. Nothing here puts an exception or a user-supplied string into an
HTTP message.

The three `/publish` routes are the exception to all of the above, on purpose:
they are called by a script holding a pack publish token (`cpk_…`), not by a
person with a login, and they sit behind their own gate (`require_pack_token`)
which accepts nothing else. That token belongs to ONE pack, so there is no other
pack to hide; what it can do is what its maker can do to the pack right now.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    client_ip,
    code_readable_repo_slugs,
    current_workspace_id,
    require_codebase_reader,
    require_pack_author,
    require_pack_token,
    require_signed_in,
)
from src.db.models import Workspace
from src.db.session import get_async_session
from src.knowledge import brief as brief_mod
from src.knowledge import ingest, store
from src.knowledge.timestamps import iso_utc
from src.knowledge.visibility import (
    PACK_NOT_FOUND,
    PackNotAccessible,
    PackPrincipal,
    can_create,
    principal_for,
)
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/codebase", tags=["codebase"])

#: How much text the REST brief may return.
BRIEF_BUDGET_CHARS = 4000
SEARCH_LIMIT = 50


# ─── Wire shapes ─────────────────────────────────────────────────────


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TeamGrantIn(_In):
    team_id: str = Field(min_length=1, max_length=64)
    permission: str = Field(default="read", max_length=16)


class PackCreate(_In):
    slug: str = Field(min_length=1, max_length=store.MAX_SLUG)
    title: str = Field(min_length=1, max_length=store.MAX_TITLE)
    description: str = Field(default="", max_length=store.MAX_DESCRIPTION)
    audience: str = Field(default="teams", max_length=16)
    teams: list[TeamGrantIn] = Field(default_factory=list, max_length=store.MAX_TEAM_GRANTS)


class PackPatch(_In):
    title: str | None = Field(default=None, min_length=1, max_length=store.MAX_TITLE)
    description: str | None = Field(default=None, max_length=store.MAX_DESCRIPTION)
    audience: str | None = Field(default=None, max_length=16)


class TeamsIn(_In):
    teams: list[TeamGrantIn] = Field(max_length=store.MAX_TEAM_GRANTS)


class MemberIn(_In):
    email: str = Field(min_length=3, max_length=320)
    permission: str = Field(default="read", max_length=16)


class InviteIn(_In):
    expires_in_days: int = Field(default=7, ge=1, le=30)
    max_uses: int = Field(default=25, ge=1, le=500)


class TokenIn(_In):
    token: str = Field(min_length=1, max_length=store.MAX_TOKEN_CHARS)


class BriefIn(_In):
    task: str = Field(min_length=1, max_length=8000)


class PublishTokenIn(_In):
    label: str = Field(min_length=1, max_length=store.MAX_LABEL)
    expires_in_days: int = Field(default=store.DEFAULT_TOKEN_DAYS, ge=1, le=store.MAX_TOKEN_DAYS)


class VersionBrief(BaseModel):
    number: int
    created_at: str | None
    file_count: int
    sha: str


class TeamOut(BaseModel):
    id: str
    name: str
    permission: str


class PackOut(BaseModel):
    id: str
    slug: str
    title: str
    description: str
    audience: str
    current_version: VersionBrief | None
    counts: dict[str, int]
    teams: list[TeamOut]
    can_maintain: bool
    updated_at: str | None


class ItemBrief(BaseModel):
    id: str
    path: str
    kind: str
    skill: str | None
    name: str
    description: str
    size_bytes: int
    sha: str


class MemberOut(BaseModel):
    user_id: str
    email: str | None
    permission: str
    via_invite: bool


class SkillUsageOut(BaseModel):
    skill: str
    opens_30d: int
    last_used_at: str | None


class UsageOut(BaseModel):
    """What agents did with the pack in 30 days. Counts and times, no people."""

    calls_30d: int
    last_used_at: str | None
    skills: list[SkillUsageOut]


class PackDetail(PackOut):
    items: list[ItemBrief]
    members: list[MemberOut]
    usage: UsageOut | None
    created_by: str | None


class PackList(BaseModel):
    packs: list[PackOut]
    can_create: bool


class VersionOut(BaseModel):
    number: int
    note: str
    created_at: str | None
    created_by: str | None
    file_count: int
    total_bytes: int
    sha: str
    current: bool


class InviteOut(BaseModel):
    id: str
    created_at: str | None
    expires_at: str | None
    max_uses: int
    used_count: int
    revoked: bool
    created_by: str | None


class PublishTokenOut(BaseModel):
    id: str
    label: str
    created_at: str | None
    expires_at: str | None
    last_used_at: str | None
    revoked: bool
    created_by: str | None


# ─── Small helpers ───────────────────────────────────────────────────


@contextlib.contextmanager
def _answers() -> Iterator[None]:
    """Turn the store's refusals into their fixed HTTP answers."""
    try:
        yield
    except PackNotAccessible:
        raise HTTPException(status_code=404, detail=PACK_NOT_FOUND) from None
    except store.PackError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from None


def _iso(moment: datetime | None) -> str | None:
    """ISO-8601 in UTC with its offset, whatever the database returned (naive or aware)."""
    return iso_utc(moment)


def _email(user_id: str | None) -> str | None:
    """A person's address for display, or None for an id the user store lacks."""
    if not user_id:
        return None
    from src.users.store import get_user_store

    try:
        found = get_user_store().get_by_id(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("codebase_user_lookup_failed err=%s", type(exc).__name__)
        return None
    return (found.email or None) if found else None


async def _principal(session: AsyncSession, user: User, ws_id: str) -> PackPrincipal:
    return await principal_for(session, user, ws_id)


async def _open(session: AsyncSession, user: User, ws_id: str, pack_id: str) -> store.PackView:
    """Gate 2: the pack, if this person may see it."""
    principal = await _principal(session, user, ws_id)
    with _answers():
        return await store.get_pack(session, principal, ws_id, pack_id)


async def _maintained(session: AsyncSession, user: User, ws_id: str,
                      pack_id: str) -> store.PackView:
    """Gates 2 and 3: the pack, and the right to change it."""
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        store.require_maintainer(view)
    return view


def _pack_out(info: store.PackInfo) -> PackOut:
    version = info.version
    return PackOut(
        id=info.id, slug=info.slug, title=info.title, description=info.description,
        audience=info.audience,
        current_version=VersionBrief(
            number=version.number, created_at=_iso(version.created_at),
            file_count=version.file_count, sha=version.sha) if version else None,
        counts={"skills": info.skills, "files": info.files},
        teams=[TeamOut(id=t.team_id, name=t.name, permission=t.permission) for t in info.teams],
        can_maintain=info.can_maintain, updated_at=_iso(info.updated_at))


async def _described(session: AsyncSession, view: store.PackView) -> store.PackInfo:
    return (await store.describe(session, [view]))[0]


async def _fresh(session: AsyncSession, user: User, ws_id: str, pack_id: str) -> PackOut:
    """The pack as it is NOW, after a change."""
    return _pack_out(await _described(session, await _open(session, user, ws_id, pack_id)))


def _version_out(info: store.VersionInfo) -> VersionOut:
    return VersionOut(
        number=info.number, note=info.note, created_at=_iso(info.created_at),
        created_by=_email(info.created_by_id), file_count=info.file_count,
        total_bytes=info.total_bytes, sha=info.sha, current=info.current)


def _audit(action: str, *, user: User, ws_id: str, request: Request, target: str,
           detail: dict[str, Any] | None = None) -> None:
    """One audit row: ids and shapes, never a title, a path or file text (a token's
    label is the one maintainer-chosen name it carries, so the row can say which)."""
    from src.security.audit import record_action

    record_action(
        action=f"codebase.{action}", actor=user.email, actor_id=user.id, workspace_id=ws_id,
        target=target, ip=client_ip(request), detail=detail or {})


async def _readable_pointers(user: User, ws_id: str, pointers: Any) -> list[str]:
    """The repository pointers (`slug`, `slug:path`, `slug:path:line`) of an item
    whose repository the caller may read as code — the rule the brief applies;
    any other is left out without a trace. The file text itself is the file's."""
    wanted = [str(p) for p in (pointers or []) if isinstance(p, str)]
    slugs = sorted({p.split(":", 1)[0].strip() for p in wanted} - {""})
    readable = await code_readable_repo_slugs(user, ws_id, slugs[:200]) if slugs else set()
    return [p[:200] for p in wanted if p.split(":", 1)[0].strip() in readable][:6]


def _truncated(cut: bool) -> dict[str, bool]:
    """``{"truncated": true}`` when a search or brief stopped at the safety cap on
    the items it reads (see `store.MAX_RANKED_ITEMS`), nothing otherwise."""
    return {"truncated": True} if cut else {}


def _ingest_failure(exc: ingest.IngestError) -> HTTPException:
    return HTTPException(status_code=422, detail={
        "code": exc.code, "message": exc.message, "hits": list(exc.hits or [])})


# ─── Packs ───────────────────────────────────────────────────────────


@router.get("/packs", response_model=PackList)
async def list_packs(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> PackList:
    principal = await _principal(session, user, ws_id)
    views = await store.list_views(session, principal, ws_id)
    return PackList(packs=[_pack_out(i) for i in await store.describe(session, views)],
                    can_create=can_create(principal))


@router.post("/packs", response_model=PackOut, status_code=201)
async def create_pack(
    body: PackCreate,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_pack_author),
    ws_id: str = Depends(current_workspace_id),
) -> PackOut:
    principal = await _principal(session, user, ws_id)
    with _answers():
        view = await store.create_pack(
            session, principal, ws_id, slug=body.slug, title=body.title,
            description=body.description, audience=body.audience,
            teams=[(t.team_id, t.permission) for t in body.teams])
    _audit("pack_created", user=user, ws_id=ws_id, request=request, target=view.pack.id,
           detail={"audience": view.pack.audience, "teams": len(body.teams)})
    return _pack_out(await _described(session, view))


@router.get("/packs/{pack_id}", response_model=PackDetail)
async def get_pack(
    pack_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> PackDetail:
    view = await _open(session, user, ws_id, pack_id)
    info = await _described(session, view)
    items = await store.list_items(session, view)
    members: list[MemberOut] = []
    usage: UsageOut | None = None
    if view.can_maintain:
        rows = await store.list_members(session, view)
        members = _members_out(rows)
        usage = UsageOut(**await store.usage(session, view))
    return PackDetail(
        **_pack_out(info).model_dump(),
        items=[ItemBrief(id=i.id, path=i.path, kind=i.kind, skill=i.skill, name=i.name,
                         description=i.description, size_bytes=i.size_bytes, sha=i.sha)
               for i in items],
        members=members, usage=usage, created_by=_email(view.pack.created_by_id))


@router.patch("/packs/{pack_id}", response_model=PackOut)
async def update_pack(
    pack_id: str,
    body: PackPatch,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> PackOut:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        changed = await store.update_pack(
            session, view, title=body.title, description=body.description,
            audience=body.audience)
    if changed:
        _audit("pack_updated", user=user, ws_id=ws_id, request=request, target=view.pack.id,
               detail={"fields": changed})
    return await _fresh(session, user, ws_id, pack_id)


@router.delete("/packs/{pack_id}", status_code=204)
async def delete_pack(
    pack_id: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> Response:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        await store.delete_pack(session, view)
    _audit("pack_deleted", user=user, ws_id=ws_id, request=request, target=view.pack.id)
    return Response(status_code=204)


@router.put("/packs/{pack_id}/teams", response_model=PackOut)
async def replace_teams(
    pack_id: str,
    body: TeamsIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> PackOut:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        await store.set_teams(session, view, [(t.team_id, t.permission) for t in body.teams])
    _audit("teams_changed", user=user, ws_id=ws_id, request=request, target=view.pack.id,
           detail={"teams": len(body.teams)})
    return await _fresh(session, user, ws_id, pack_id)


# ─── People ──────────────────────────────────────────────────────────


def _members_out(rows) -> list[MemberOut]:
    return [MemberOut(user_id=m.user_id, email=_email(m.user_id), permission=m.permission,
                      via_invite=m.invite_id is not None) for m in rows]


@router.post("/packs/{pack_id}/members")
async def add_member(
    pack_id: str,
    body: MemberIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, list[MemberOut]]:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        row, created = await store.put_member(session, view, body.email, body.permission)
        rows = await store.list_members(session, view)
    _audit("member_added" if created else "member_changed", user=user, ws_id=ws_id,
           request=request, target=view.pack.id,
           detail={"user_id": row.user_id, "permission": row.permission})
    return {"members": _members_out(rows)}


@router.delete("/packs/{pack_id}/members/{user_id}", status_code=204)
async def remove_member(
    pack_id: str,
    request: Request,
    user_id: str = Path(..., max_length=200),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> Response:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        await store.remove_member(session, view, user_id)
    _audit("member_removed", user=user, ws_id=ws_id, request=request, target=view.pack.id,
           detail={"user_id": user_id})
    return Response(status_code=204)


# ─── Versions ────────────────────────────────────────────────────────


@router.get("/packs/{pack_id}/versions")
async def list_versions(
    pack_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, list[VersionOut]]:
    view = await _open(session, user, ws_id, pack_id)
    return {"versions": [_version_out(v) for v in await store.list_versions(session, view)]}


async def _ingest_upload(archive: UploadFile | None, files: list[UploadFile] | None,
                         paths: list[str] | None) -> ingest.IngestResult:
    """The upload of a version, from ONE zip (`archive`) or from files (`files`,
    each named by its relative path — or by the matching entry of `paths`, when
    sent), checked by `ingest`. Raises the 422 an upload that is refused gets."""
    if (archive is None) == (not files):
        raise HTTPException(status_code=422, detail={
            "code": "bad_upload", "message": "Send either one archive or a list of files.",
            "hits": []})
    try:
        if archive is not None:
            data = await archive.read(ingest.MAX_ARCHIVE_BYTES + 1)
            return await asyncio.to_thread(ingest.ingest_zip, data)
        names = list(paths or [])
        if names and len(names) != len(files or []):
            raise HTTPException(status_code=422, detail={
                "code": "bad_upload", "message": "`paths` must match `files` one to one.",
                "hits": []})
        entries = []
        for index, part in enumerate(files or []):
            body = await part.read(ingest.MAX_FILE_BYTES + 1)
            entries.append((names[index] if names else (part.filename or ""), body))
        return await asyncio.to_thread(ingest.ingest_files, entries)
    except ingest.IngestError as exc:
        raise _ingest_failure(exc) from None
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — last resort: a bad archive is a 400, never a 500
        logger.warning("codebase_upload_unreadable err=%s", type(exc).__name__)
        raise HTTPException(status_code=400, detail={
            "code": "bad_archive", "message": "The upload could not be read as a pack.",
            "hits": []}) from None


def _uploaded(version, result: ingest.IngestResult) -> dict[str, Any]:
    info = store.VersionInfo(
        version.id, version.number, version.note, version.created_at, version.created_by_id,
        version.file_count, version.total_bytes, version.sha, True)
    return {"version": _version_out(info), "ignored": result.ignored, "skills": result.skills,
            "ignored_total": result.ignored_total, "allowed": result.allowed}


def _uploaded_detail(version, result: ingest.IngestResult, **extra: Any) -> dict[str, Any]:
    """The audit detail of an upload: counts only. `allowed_count` is how many
    lines were accepted only because they carry the allow marker."""
    return {"number": version.number, "file_count": version.file_count,
            "total_bytes": version.total_bytes, "allowed_count": len(result.allowed), **extra}


@router.post("/packs/{pack_id}/versions", status_code=201)
async def upload_version(
    pack_id: str,
    request: Request,
    archive: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
    paths: list[str] | None = Form(default=None),
    note: str = Form(default="", max_length=store.MAX_NOTE),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """A new version from ONE zip (`archive`) or from files (`files`, each named
    by its relative path — or by the matching entry of `paths`, when sent)."""
    view = await _maintained(session, user, ws_id, pack_id)     # before any byte is read
    result = await _ingest_upload(archive, files, paths)
    with _answers():
        version = await store.add_version(session, view, result, note)
    _audit("version_uploaded", user=user, ws_id=ws_id, request=request, target=view.pack.id,
           detail=_uploaded_detail(version, result))
    return _uploaded(version, result)


@router.post("/packs/{pack_id}/versions/{number}/activate", response_model=PackOut)
async def activate_version(
    pack_id: str,
    request: Request,
    number: int = Path(..., ge=1, le=1_000_000),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> PackOut:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        await store.activate_version(session, view, number)
    _audit("version_activated", user=user, ws_id=ws_id, request=request, target=view.pack.id,
           detail={"number": number})
    return await _fresh(session, user, ws_id, pack_id)


@router.get("/packs/{pack_id}/versions/{number}/diff")
async def diff_version(
    pack_id: str,
    number: int = Path(..., ge=1, le=1_000_000),
    against: int | None = Query(default=None, ge=1, le=1_000_000),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, list[str]]:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        return await store.diff_versions(session, view, number, against)


@router.get("/packs/{pack_id}/versions/{number}/archive")
async def download_version(
    pack_id: str,
    number: int = Path(..., ge=1, le=1_000_000),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> Response:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        data, name = await store.version_archive(session, view, number)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/packs/{pack_id}/items/{item_id}")
async def read_item(
    pack_id: str,
    item_id: str = Path(..., max_length=64),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        item, number = await store.get_item(session, view, item_id)
    return {"id": item.id, "path": item.path, "kind": item.kind, "skill": item.skill,
            "name": item.name, "description": item.description, "content": item.content,
            "repos": await _readable_pointers(user, ws_id, item.repos),
            "sha": item.sha, "size_bytes": item.size_bytes, "version": number}


# ─── Invite links ────────────────────────────────────────────────────


def _invite_out(row) -> InviteOut:
    return InviteOut(
        id=row.id, created_at=_iso(row.created_at), expires_at=_iso(row.expires_at),
        max_uses=row.max_uses, used_count=row.used_count, revoked=row.revoked_at is not None,
        created_by=_email(row.created_by_id))


@router.get("/packs/{pack_id}/invites")
async def list_invites(
    pack_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, list[InviteOut]]:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        rows = await store.list_invites(session, view)
    return {"invites": [_invite_out(r) for r in rows]}


@router.post("/packs/{pack_id}/invites", status_code=201)
async def create_invite(
    pack_id: str,
    body: InviteIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        row, token = await store.create_invite(
            session, view, expires_in_days=body.expires_in_days, max_uses=body.max_uses)
    _audit("invite_created", user=user, ws_id=ws_id, request=request, target=view.pack.id,
           detail={"invite_id": row.id, "max_uses": row.max_uses,
                   "expires_in_days": body.expires_in_days})
    return {"invite": _invite_out(row), "token": token, "path": f"/codebase/join/{token}"}


@router.delete("/packs/{pack_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(
    pack_id: str,
    request: Request,
    invite_id: str = Path(..., max_length=64),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> Response:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        await store.revoke_invite(session, view, invite_id)
    _audit("invite_revoked", user=user, ws_id=ws_id, request=request, target=view.pack.id,
           detail={"invite_id": invite_id})
    return Response(status_code=204)


# ─── Publish tokens (managed by maintainers) ─────────────────────────


def _token_out(row) -> PublishTokenOut:
    return PublishTokenOut(
        id=row.id, label=row.label, created_at=_iso(row.created_at),
        expires_at=_iso(row.expires_at), last_used_at=_iso(row.last_used_at),
        revoked=row.revoked_at is not None, created_by=_email(row.created_by_id))


@router.get("/packs/{pack_id}/tokens")
async def list_tokens(
    pack_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, list[PublishTokenOut]]:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        rows = await store.list_tokens(session, view)
    return {"tokens": [_token_out(r) for r in rows]}


@router.post("/packs/{pack_id}/tokens", status_code=201)
async def create_token(
    pack_id: str,
    body: PublishTokenIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """A token that publishes this pack from a script. Returned HERE ONLY."""
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        row, token = await store.create_token(
            session, view, label=body.label, expires_in_days=body.expires_in_days)
    _audit("token_created", user=user, ws_id=ws_id, request=request, target=view.pack.id,
           detail={"token_id": row.id, "label": row.label})
    return {"token_info": _token_out(row), "token": token}


@router.delete("/packs/{pack_id}/tokens/{token_id}", status_code=204)
async def revoke_token(
    pack_id: str,
    request: Request,
    token_id: str = Path(..., max_length=64),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> Response:
    view = await _open(session, user, ws_id, pack_id)
    with _answers():
        row = await store.revoke_token(session, view, token_id)
    _audit("token_revoked", user=user, ws_id=ws_id, request=request, target=view.pack.id,
           detail={"token_id": row.id, "label": row.label})
    return Response(status_code=204)


@router.post("/invites/preview")
async def preview_invite(
    body: TokenIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_signed_in),
) -> dict[str, Any]:
    with _answers():
        info = await store.preview_invite(session, user, body.token)
        workspace = await session.get(Workspace, info.workspace_id)
        if workspace is None:
            raise store.InviteNotFound()
    return {"pack_title": info.pack_title, "pack_description": info.pack_description,
            "workspace_slug": workspace.slug, "workspace_name": workspace.name,
            "already_member": info.already_member}


@router.post("/invites/redeem")
async def redeem_invite(
    body: TokenIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_signed_in),
) -> dict[str, str]:
    with _answers():
        pack, joined = await store.redeem_invite(session, user, body.token)
        workspace = await session.get(Workspace, pack.workspace_id)
        if workspace is None:
            raise store.InviteNotFound()
    if joined:
        _audit("invite_redeemed", user=user, ws_id=pack.workspace_id, request=request,
               target=pack.id)
    return {"pack_id": pack.id, "workspace_slug": workspace.slug}


# ─── Publishing with a pack token (a laptop or a pipeline) ───────────


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _file_row(item) -> dict[str, Any]:
    return {"path": item.path, "sha": item.sha, "size_bytes": item.size_bytes,
            "kind": item.kind, "skill": item.skill}


@router.get("/publish")
async def publish_status(
    session: AsyncSession = Depends(get_async_session),
    auth: store.PublishAuth = Depends(require_pack_token),
) -> dict[str, Any]:
    """The pack this token publishes: its current version, the sha of every file
    in it (so a script can tell what changed without downloading it) and how it
    is being used."""
    view = auth.view
    info = await _described(session, view)
    items = await store.list_items(session, view)
    version = info.version
    return {
        "pack": {"id": info.id, "slug": info.slug, "title": info.title,
                 "audience": info.audience},
        "current_version": VersionBrief(
            number=version.number, created_at=_iso(version.created_at),
            file_count=version.file_count, sha=version.sha) if version else None,
        "files": [_file_row(i) for i in items],
        "usage": UsageOut(**await store.usage(session, view)),
    }


@router.post("/publish")
async def publish_version(
    request: Request,
    archive: UploadFile | None = File(default=None),
    files: list[UploadFile] | None = File(default=None),
    paths: list[str] | None = Form(default=None),
    note: str = Form(default="", max_length=store.MAX_NOTE),
    dry_run: str = Form(default="", max_length=8),
    session: AsyncSession = Depends(get_async_session),
    auth: store.PublishAuth = Depends(require_pack_token),
) -> Response:
    """A new version from a script: the same upload and the same checks as the
    pack page. 201 with the new version; 200 `{"dry_run": true, …}` and nothing
    stored for a dry run; 200 `{"unchanged": true, …}` and no new version when the
    files are exactly the current version's (a pipeline that pushes on every
    commit must not push the versions worth rolling back to out of the 20 kept)."""
    view = auth.view
    result = await _ingest_upload(archive, files, paths)
    sha = ingest.version_sha(result.files)
    if _truthy(dry_run):
        return JSONResponse(status_code=200, content={
            "dry_run": True, "sha": sha, "skills": result.skills, "ignored": result.ignored,
            "ignored_total": result.ignored_total, "allowed": result.allowed,
            "files": [_file_row(f) for f in result.files]})
    current = await store.current_version(session, view)
    if current is not None and current.sha == sha:
        info = store.VersionInfo(
            current.id, current.number, current.note, current.created_at,
            current.created_by_id, current.file_count, current.total_bytes, current.sha, True)
        return JSONResponse(status_code=200, content=jsonable_encoder({
            "unchanged": True, "version": _version_out(info), "ignored": result.ignored,
            "skills": result.skills, "ignored_total": result.ignored_total,
            "allowed": result.allowed}))
    with _answers():
        version = await store.add_version(session, view, result, note)
    _audit("version_uploaded", user=auth.user, ws_id=view.ws_id, request=request,
           target=view.pack.id, detail=_uploaded_detail(version, result, token_id=auth.token.id))
    return JSONResponse(status_code=201, content=jsonable_encoder(_uploaded(version, result)))


@router.get("/publish/archive")
async def publish_archive(
    session: AsyncSession = Depends(get_async_session),
    auth: store.PublishAuth = Depends(require_pack_token),
) -> Response:
    """The zip of the pack's CURRENT version (for `celmis-pack pull`)."""
    view = auth.view
    current = await store.current_version(session, view)
    if current is None:
        raise HTTPException(status_code=404, detail=store.VersionNotFound.message)
    with _answers():
        data, name = await store.version_archive(session, view, current.number)
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ─── Search and the brief ────────────────────────────────────────────


@router.get("/search")
async def search_packs(
    q: str = Query(default="", max_length=300),
    limit: int = Query(default=20, ge=1, le=SEARCH_LIMIT),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    principal = await _principal(session, user, ws_id)
    hits = await store.search(session, principal, ws_id, q, limit)
    return {"hits": [{
        "pack_id": h.pack.id, "pack_slug": h.pack.slug, "pack_title": h.pack.title,
        "item_id": h.item.id, "path": h.item.path, "kind": h.item.kind, "skill": h.item.skill,
        "name": h.item.name, "snippet": h.snippet, "score": round(h.score, 4)} for h in hits],
        **_truncated(hits.truncated)}


@router.post("/brief")
async def brief(
    body: BriefIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_codebase_reader),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    principal = await _principal(session, user, ws_id)
    ranking = await store.rank_current(session, principal, ws_id, body.task,
                                       limit=brief_mod.POOL, per_kind=brief_mod.LIMITS)
    plan = brief_mod.plan_from_hits(body.task, ranking.hits, ranking.meta)
    if plan is None:
        return {"text": brief_mod.NO_MATCH, "items": [], **_truncated(ranking.truncated)}
    # Repo pointers the caller may read as code; any other is dropped.
    pointers = [r for note in plan.chosen["note"] for r in note.repos]
    slugs = sorted({str(p).split(":", 1)[0].strip() for p in pointers} - {""})
    readable = await code_readable_repo_slugs(user, ws_id, slugs[:200]) if slugs else set()
    texts = await store.item_contents(session, principal, ws_id, plan.quoted_ids)
    result = brief_mod.render_brief(plan, texts, lambda slug: slug in readable,
                                    BRIEF_BUDGET_CHARS)
    return {"text": result.text, "items": [
        {"pack_slug": plan.meta[i][0].slug, "item_id": i, "path": plan.meta[i][2].path,
         "kind": plan.meta[i][2].kind, "name": plan.meta[i][2].name}
        for i in result.item_ids if i in plan.meta], **_truncated(ranking.truncated)}


__all__ = ["router"]
