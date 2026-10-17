"""Reading and writing knowledge packs — every query scoped and filtered here.

The router and the MCP tools call this module and nothing else touches the
pack tables. Three rules hold throughout:

* Every query carries the workspace id, and every pack read starts from
  :func:`src.knowledge.visibility.visible_packs_query`, so a hidden pack is
  never loaded. What the SQL returned is then asked of the pure predicate
  again (:func:`_views`); a disagreement drops the pack.
* Hidden is missing: whatever the reason a pack cannot be shown, the answer is
  :class:`PackNotAccessible`, and a malformed id is answered the same way.
* No foreign key is relied on. Deleting a pack, a team, a workspace or a person
  deletes the child rows here, explicitly. The cleanup helpers add to the
  caller's transaction and never commit; every other write commits.

Errors are :class:`PackError` subclasses carrying a fixed status and a fixed
sentence — nothing in a message is built from user input or from an exception.

Synchronous twins (:func:`visible_current_items_sync`, :func:`find_skill_sync`,
:func:`pack_items_sync`, :func:`item_contents_sync`) serve the MCP tools, which
run without a request session; they take a principal built by
``principal_for_sync`` and apply the same filter.
"""

from __future__ import annotations

import hashlib
import heapq
import io
import logging
import math
import re
import secrets
import unicodedata
import uuid
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import and_, delete, event, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.db.models import (
    KnowledgeItem,
    KnowledgePack,
    KnowledgePackInvite,
    KnowledgePackMember,
    KnowledgePackTeam,
    KnowledgePackToken,
    KnowledgePackVersion,
    Team,
    WorkspaceMember,
)
from src.knowledge import search as search_mod
from src.knowledge.visibility import (
    MAINTAIN,
    PackNotAccessible,
    PackPrincipal,
    can_create,
    can_maintain,
    can_see,
    visible_packs_query,
)
from src.users.roles import (
    PACK_AUDIENCES,
    PACK_MAINTAINER_ROLES,
    PACK_PERMISSIONS,
    VALID_WORKSPACE_ROLES,
)

logger = logging.getLogger(__name__)

# ─── Limits ──────────────────────────────────────────────────────────

MAX_PACKS_PER_WORKSPACE = 200
MAX_ACTIVE_INVITES = 20
MAX_MEMBERS = 500
MAX_TEAM_GRANTS = 100
KEEP_VERSIONS = 20
MAX_ACTIVE_TOKENS = 10
MAX_LABEL = 80
DEFAULT_TOKEN_DAYS = 90
MAX_TOKEN_DAYS = 365
#: The most items one search, brief or listing reads. A safety cap, far above
#: what a workspace may hold (200 packs of 400 files is 80000): reaching it is
#: logged, and the answer says it was cut (`truncated`).
MAX_RANKED_ITEMS = 200_000
#: Rows read from the database at a time while ranking or listing.
SCAN_BATCH = 500

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG = 64
MAX_TITLE = 200
MAX_DESCRIPTION = 2000
MAX_NOTE = 500


# ─── Errors ──────────────────────────────────────────────────────────


class PackError(Exception):
    """A refusal with a fixed HTTP status and a fixed sentence."""

    status = 400
    message = "bad request"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or type(self).message
        super().__init__(self.message)


class NotMaintainer(PackError):
    status, message = 403, "Requires a pack maintainer"


class NotAuthor(PackError):
    status, message = 403, "Requires the editor role or higher to create packs"


class SlugTaken(PackError):
    status, message = 409, "slug is not available"


class LimitReached(PackError):
    status = 409


class Conflict(PackError):
    status = 409


class TeamNotFound(PackError):
    status, message = 404, "team not found"


class PersonNotFound(PackError):
    status, message = 404, "person not found in this workspace"


class MemberNotFound(PackError):
    status, message = 404, "member not found"


class VersionNotFound(PackError):
    status, message = 404, "version not found"


class ItemNotFound(PackError):
    status, message = 404, "item not found"


class InviteNotFound(PackError):
    status, message = 404, "invite not found or expired"


class TokenNotFound(PackError):
    status, message = 404, "token not found"


class BadInput(PackError):
    status = 422


class LockOut(PackError):
    status = 422
    message = "That change would leave you without access to the pack."


# ─── Shapes ──────────────────────────────────────────────────────────


class PackRef(NamedTuple):
    """What a reader of the current content needs to name a pack."""

    id: str
    workspace_id: str
    slug: str
    title: str
    description: str
    audience: str


class ItemRef(NamedTuple):
    """One item without its text (the text is fetched for few items only)."""

    id: str
    pack_id: str
    version_id: str
    path: str
    kind: str
    skill: str | None
    name: str
    description: str
    size_bytes: int
    sha: str
    repos: list
    tags: list
    terms: dict


class SkillBundle(NamedTuple):
    pack: PackRef
    version: int
    skill: ItemRef
    content: str                   # the SKILL.md text
    files: list[ItemRef]           # every item of the skill, SKILL.md included
    directory: str                 # the folder holding SKILL.md ("" at the root)


class SearchHit(NamedTuple):
    pack: PackRef
    version: int
    item: ItemRef
    score: float
    snippet: str


@dataclass(frozen=True)
class VersionInfo:
    id: str
    number: int
    note: str
    created_at: datetime | None
    created_by_id: str | None
    file_count: int
    total_bytes: int
    sha: str
    current: bool = False


@dataclass(frozen=True)
class TeamGrantInfo:
    team_id: str
    name: str
    permission: str


@dataclass
class PackView:
    """A pack the principal may see, with the standing that decides what they
    may do to it."""

    pack: KnowledgePack
    grants: list[KnowledgePackTeam]
    member: KnowledgePackMember | None
    can_maintain: bool
    principal: PackPrincipal
    ws_id: str


@dataclass
class PackInfo:
    id: str
    slug: str
    title: str
    description: str
    audience: str
    created_by_id: str | None
    created_at: datetime | None
    updated_at: datetime | None
    version: VersionInfo | None
    skills: int
    files: int
    teams: list[TeamGrantInfo] = field(default_factory=list)
    can_maintain: bool = False


# ─── Small helpers ───────────────────────────────────────────────────


def valid_id(value: Any) -> str | None:
    """The canonical uuid string, or None for anything else. Ids are uuid
    columns: a malformed one must never reach the database, where it would
    raise instead of finding nothing — and that difference would tell a hidden
    pack from a missing one."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _aware(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _pack_ref(pack: Any) -> PackRef:
    return PackRef(pack.id, pack.workspace_id, pack.slug, pack.title,
                   pack.description or "", pack.audience)


_ITEM_COLUMNS = (
    KnowledgeItem.id, KnowledgeItem.pack_id, KnowledgeItem.version_id, KnowledgeItem.path,
    KnowledgeItem.kind, KnowledgeItem.skill, KnowledgeItem.name, KnowledgeItem.description,
    KnowledgeItem.size_bytes, KnowledgeItem.sha, KnowledgeItem.repos, KnowledgeItem.tags,
    KnowledgeItem.terms,
)


def _item_ref(row: Any) -> ItemRef:
    return ItemRef(row.id, row.pack_id, row.version_id, row.path, row.kind, row.skill,
                   row.name, row.description or "", row.size_bytes or 0, row.sha,
                   list(row.repos or []), list(row.tags or []),
                   dict(getattr(row, "terms", None) or {}))


def _version_info(row: Any, current_id: str | None) -> VersionInfo:
    return VersionInfo(row.id, row.number, row.note or "", row.created_at, row.created_by_id,
                       row.file_count, row.total_bytes, row.sha, row.id == current_id)


# ─── Loading packs: the filter, then the predicate again ─────────────


def _group(grants: Iterable[Any], members: Iterable[Any]) -> tuple[dict, dict]:
    by_pack: dict[str, list] = {}
    for g in grants:
        by_pack.setdefault(g.pack_id, []).append(g)
    return by_pack, {m.pack_id: m for m in members}


def _views(principal: PackPrincipal, ws_id: str, packs: Sequence[KnowledgePack],
           grants: dict, members: dict) -> list[PackView]:
    """Keep the packs the PURE predicate also lets the principal see — the SQL
    filter already did, so a pack dropped here means the two disagree and the
    safe answer is to show nothing."""
    out: list[PackView] = []
    for pack in packs:
        g, m = grants.get(pack.id, []), members.get(pack.id)
        if not can_see(principal, pack, g, m):
            logger.warning("pack_filter_disagreement pack=%s", pack.id)
            continue
        out.append(PackView(pack, g, m, can_maintain(principal, pack, g, m), principal, ws_id))
    return out


def _grant_query(ids: Sequence[str], ws_id: str):
    # INNER JOIN on Team in this workspace: a grant to a deleted or foreign team
    # is nothing.
    return (select(KnowledgePackTeam)
            .join(Team, Team.id == KnowledgePackTeam.team_id)
            .where(KnowledgePackTeam.pack_id.in_(list(ids)), Team.workspace_id == ws_id))


def _member_query(ids: Sequence[str], user_id: str):
    return select(KnowledgePackMember).where(
        KnowledgePackMember.pack_id.in_(list(ids)), KnowledgePackMember.user_id == user_id)


async def _load_views(session: AsyncSession, principal: PackPrincipal, ws_id: str,
                      packs: Sequence[KnowledgePack]) -> list[PackView]:
    if not packs:
        return []
    ids = [p.id for p in packs]
    grants, members = _group(
        (await session.scalars(_grant_query(ids, ws_id))).all(),
        (await session.scalars(_member_query(ids, principal.user_id))).all())
    return _views(principal, ws_id, packs, grants, members)


async def get_pack(session: AsyncSession, principal: PackPrincipal, ws_id: str,
                   pack_id: str) -> PackView:
    """The pack, if the principal may see it; :class:`PackNotAccessible` for a
    pack that does not exist, is another workspace's, is hidden or whose id is
    not an id."""
    pid = valid_id(pack_id)
    if pid is None:
        raise PackNotAccessible()
    row = await session.scalar(
        visible_packs_query(principal, ws_id).where(KnowledgePack.id == pid)
        .execution_options(populate_existing=True))
    if row is None:
        raise PackNotAccessible()
    views = await _load_views(session, principal, ws_id, [row])
    if not views:
        raise PackNotAccessible()
    return views[0]


def require_maintainer(view: PackView) -> None:
    if not view.can_maintain:
        raise NotMaintainer()


async def list_views(session: AsyncSession, principal: PackPrincipal,
                     ws_id: str) -> list[PackView]:
    rows = (await session.scalars(
        visible_packs_query(principal, ws_id).execution_options(populate_existing=True)
    )).all()
    views = await _load_views(session, principal, ws_id, rows)
    return sorted(views, key=lambda v: (v.pack.title.casefold(), v.pack.slug))


async def describe(session: AsyncSession, views: Sequence[PackView]) -> list[PackInfo]:
    """PackInfo for each view: the current version, how many skills, and — for
    maintainers only — the teams it is granted to."""
    if not views:
        return []
    current = [v.pack.current_version_id for v in views if v.pack.current_version_id]
    versions = {}
    skills: dict[str, int] = {}
    if current:
        versions = {r.id: r for r in (await session.scalars(
            select(KnowledgePackVersion).where(KnowledgePackVersion.id.in_(current)))).all()}
        skills = dict((await session.execute(
            select(KnowledgeItem.version_id, func.count())
            .where(KnowledgeItem.version_id.in_(current), KnowledgeItem.kind == "skill")
            .group_by(KnowledgeItem.version_id))).tuples().all())
    team_ids = {g.team_id for v in views if v.can_maintain for g in v.grants}
    names: dict[str, str] = {}
    if team_ids:
        names = dict((await session.execute(
            select(Team.id, Team.name).where(Team.id.in_(sorted(team_ids))))).tuples().all())
    out = []
    for v in views:
        p = v.pack
        ver = versions.get(p.current_version_id) if p.current_version_id else None
        out.append(PackInfo(
            id=p.id, slug=p.slug, title=p.title, description=p.description or "",
            audience=p.audience, created_by_id=p.created_by_id, created_at=p.created_at,
            updated_at=p.updated_at,
            version=_version_info(ver, p.current_version_id) if ver else None,
            skills=skills.get(p.current_version_id, 0) if ver else 0,
            files=ver.file_count if ver else 0,
            teams=([TeamGrantInfo(g.team_id, names.get(g.team_id, ""), g.permission)
                    for g in sorted(v.grants, key=lambda g: names.get(g.team_id, ""))]
                   if v.can_maintain else []),
            can_maintain=v.can_maintain))
    return out


# ─── Creating, changing, deleting ────────────────────────────────────


#: Bidirectional override and isolate controls: they reorder what a reader sees
#: without changing what is stored, so a title can read as something it is not.
#: The other format characters (a joiner inside an emoji) are ordinary text.
_BIDI_CONTROLS = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))


def _check_text(value: str, limit: int, what: str, *, required: bool = False,
                multiline: bool = False) -> str:
    """A trimmed, bounded line of text with no control characters in it.

    A control character (NUL, an escape sequence, a stray carriage return) is
    refused with the 422 every other bad field gets: PostgreSQL cannot store
    NUL at all, and the rest would reach a terminal or an agent as-is. A
    ``multiline`` field (a description, a note) may keep line breaks and tabs,
    and a CRLF pair is folded into one line break first. Bidirectional override
    and isolate controls are refused everywhere.
    """
    value = value or ""
    if multiline:
        value = value.replace("\r\n", "\n")
    allowed = "\n\t" if multiline else ""
    if any((unicodedata.category(ch) == "Cc" and ch not in allowed) or ch in _BIDI_CONTROLS
           for ch in value):
        raise BadInput(f"{what} must not contain control characters.")
    value = value.strip()
    if required and not value:
        raise BadInput(f"{what} is required.")
    if len(value) > limit:
        raise BadInput(f"{what} is too long.")
    return value


def _check_audience(audience: str) -> str:
    if audience not in PACK_AUDIENCES:
        raise BadInput("audience must be workspace, teams or restricted.")
    return audience


def _check_permission(permission: str) -> str:
    if permission not in PACK_PERMISSIONS:
        raise BadInput("permission must be read or maintain.")
    return permission


async def _team_grants(session: AsyncSession, ws_id: str,
                       teams: Sequence[tuple[str, str]]) -> dict[str, str]:
    """team_id -> permission for teams that all belong to THIS workspace."""
    wanted: dict[str, str] = {}
    for team_id, permission in teams:
        tid = (team_id or "").strip()
        if not tid:
            raise TeamNotFound()
        wanted[tid] = _check_permission(permission)
    if len(wanted) > MAX_TEAM_GRANTS:
        raise BadInput("Too many teams.")
    if wanted:
        found = set((await session.scalars(
            select(Team.id).where(Team.id.in_(sorted(wanted)), Team.workspace_id == ws_id))).all())
        if found != set(wanted):
            raise TeamNotFound()
    return wanted


async def _keep_actor(session: AsyncSession, principal: PackPrincipal,
                      pack: KnowledgePack) -> bool:
    """After a change to who the pack is for, make sure the person who made it
    still maintains it — by naming them a maintainer if they would not. False
    when that is impossible (they hold no role in the workspace)."""
    if principal.superadmin:
        return True
    await session.flush()
    grants = (await session.scalars(_grant_query([pack.id], pack.workspace_id))).all()
    member = await session.get(KnowledgePackMember, (pack.id, principal.user_id),
                               populate_existing=True)
    if can_maintain(principal, pack, grants, member):
        return True
    if member is None:
        member = KnowledgePackMember(pack_id=pack.id, user_id=principal.user_id,
                                     permission=MAINTAIN, granted_by_id=principal.user_id)
        session.add(member)
    else:
        member.permission = MAINTAIN
    await session.flush()
    return can_maintain(principal, pack, grants, member)


async def create_pack(session: AsyncSession, principal: PackPrincipal, ws_id: str, *,
                      slug: str, title: str, description: str = "", audience: str = "teams",
                      teams: Sequence[tuple[str, str]] = ()) -> PackView:
    if not can_create(principal):
        raise NotAuthor()
    slug = (slug or "").strip().lower()
    if not slug or len(slug) > MAX_SLUG or not SLUG_RE.match(slug):
        raise BadInput("slug must be lowercase letters, digits and single dashes.")
    title = _check_text(title, MAX_TITLE, "title", required=True)
    description = _check_text(description, MAX_DESCRIPTION, "description", multiline=True)
    audience = _check_audience(audience)
    grants = await _team_grants(session, ws_id, teams)
    total = await session.scalar(
        select(func.count()).select_from(KnowledgePack).where(KnowledgePack.workspace_id == ws_id))
    if (total or 0) >= MAX_PACKS_PER_WORKSPACE:
        raise LimitReached(f"A workspace holds at most {MAX_PACKS_PER_WORKSPACE} packs.")
    # A clash with a pack the caller cannot see answers like one they can.
    if await session.scalar(select(KnowledgePack.id).where(
            KnowledgePack.workspace_id == ws_id, KnowledgePack.slug == slug)):
        raise SlugTaken()
    pack = KnowledgePack(workspace_id=ws_id, slug=slug, title=title, description=description,
                         audience=audience, created_by_id=principal.user_id)
    session.add(pack)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise SlugTaken() from None
    for team_id, permission in grants.items():
        session.add(KnowledgePackTeam(pack_id=pack.id, team_id=team_id, permission=permission,
                                      granted_by_id=principal.user_id))
    # The creator always maintains what they made.
    if principal.role in VALID_WORKSPACE_ROLES:
        session.add(KnowledgePackMember(pack_id=pack.id, user_id=principal.user_id,
                                        permission=MAINTAIN, granted_by_id=principal.user_id))
    if not await _keep_actor(session, principal, pack):
        await session.rollback()
        raise LockOut("A restricted pack needs a team or a person that can open it; "
                      "join the workspace first.")
    await session.commit()
    return await get_pack(session, principal, ws_id, pack.id)


async def update_pack(session: AsyncSession, view: PackView, *, title: str | None = None,
                      description: str | None = None, audience: str | None = None) -> list[str]:
    """Change the pack's fields; returns the names that changed."""
    require_maintainer(view)
    pack, changed = view.pack, []
    if title is not None:
        title = _check_text(title, MAX_TITLE, "title", required=True)
        if title != pack.title:
            pack.title = title
            changed.append("title")
    if description is not None:
        description = _check_text(description, MAX_DESCRIPTION, "description", multiline=True)
        if description != (pack.description or ""):
            pack.description = description
            changed.append("description")
    if audience is not None:
        audience = _check_audience(audience)
        if audience != pack.audience:
            pack.audience = audience
            changed.append("audience")
    if "audience" in changed and not await _keep_actor(session, view.principal, pack):
        await session.rollback()
        raise LockOut()
    if changed:
        await session.commit()
    return changed


async def set_teams(session: AsyncSession, view: PackView,
                    teams: Sequence[tuple[str, str]]) -> None:
    """Replace the pack's team grants with ``teams`` (``[(team_id, permission)]``)."""
    require_maintainer(view)
    grants = await _team_grants(session, view.ws_id, teams)
    await session.execute(delete(KnowledgePackTeam).where(
        KnowledgePackTeam.pack_id == view.pack.id))
    for team_id, permission in grants.items():
        session.add(KnowledgePackTeam(pack_id=view.pack.id, team_id=team_id,
                                      permission=permission,
                                      granted_by_id=view.principal.user_id))
    if not await _keep_actor(session, view.principal, view.pack):
        await session.rollback()
        raise LockOut()
    await session.commit()


def _pack_row_deletes(pack_ids: Sequence[str]) -> list[Any]:
    """Children first, then the packs — no cascade is assumed."""
    ids = list(pack_ids)
    if not ids:
        return []
    out: list[Any] = [delete(model).where(model.pack_id.in_(ids)) for model in (
        KnowledgeItem, KnowledgePackVersion, KnowledgePackInvite, KnowledgePackToken,
        KnowledgePackMember, KnowledgePackTeam)]
    out.append(delete(KnowledgePack).where(KnowledgePack.id.in_(ids)))
    return out


async def _delete_pack_rows(session: AsyncSession, pack_ids: Sequence[str]) -> None:
    for statement in _pack_row_deletes(pack_ids):
        await session.execute(statement)


async def delete_pack(session: AsyncSession, view: PackView) -> None:
    require_maintainer(view)
    await _delete_pack_rows(session, [view.pack.id])
    await session.commit()


# ─── Explicit members ────────────────────────────────────────────────


async def list_members(session: AsyncSession, view: PackView) -> list[KnowledgePackMember]:
    require_maintainer(view)
    rows = (await session.scalars(select(KnowledgePackMember).where(
        KnowledgePackMember.pack_id == view.pack.id))).all()
    return sorted(rows, key=lambda m: (m.created_at is None, m.created_at, m.user_id))


async def _workspace_user_by_email(session: AsyncSession, ws_id: str, email: str) -> tuple[str, str]:
    """The id and the role of the CURRENT member of this workspace with this
    email. An address that belongs to an account elsewhere reads exactly like no
    account."""
    from src.users.store import get_user_store

    email = (email or "").strip()
    user = get_user_store().get_by_email(email) if "@" in email else None
    if user is None or not getattr(user, "is_active", True):
        raise PersonNotFound()
    member = await session.get(WorkspaceMember, (ws_id, user.id))
    if member is None or member.role not in VALID_WORKSPACE_ROLES:
        raise PersonNotFound()
    return user.id, member.role


async def put_member(session: AsyncSession, view: PackView, email: str,
                     permission: str) -> tuple[KnowledgePackMember, bool]:
    """Name a person on the pack, or change what they hold on it. Returns the row
    and whether it is NEW (so the route can audit an addition and a change apart)."""
    require_maintainer(view)
    permission = _check_permission(permission)
    user_id, role = await _workspace_user_by_email(session, view.ws_id, email)
    if permission == MAINTAIN and role not in PACK_MAINTAINER_ROLES:
        raise BadInput("A viewer cannot maintain a pack; give them read access, "
                       "or change their workspace role first.")
    row = await session.get(KnowledgePackMember, (view.pack.id, user_id), populate_existing=True)
    created = row is None
    if row is None:
        count = await session.scalar(select(func.count()).select_from(KnowledgePackMember)
                                     .where(KnowledgePackMember.pack_id == view.pack.id))
        if (count or 0) >= MAX_MEMBERS:
            raise LimitReached(f"A pack names at most {MAX_MEMBERS} people.")
        row = KnowledgePackMember(pack_id=view.pack.id, user_id=user_id, permission=permission,
                                  granted_by_id=view.principal.user_id)
        session.add(row)
    else:
        if permission != MAINTAIN and row.user_id == view.principal.user_id:
            await _require_another_maintainer(session, view)
        row.permission = permission
    await session.commit()
    return row, created


async def add_member(session: AsyncSession, view: PackView, email: str,
                     permission: str) -> KnowledgePackMember:
    return (await put_member(session, view, email, permission))[0]


async def _require_another_maintainer(session: AsyncSession, view: PackView) -> None:
    """A restricted pack is maintained only by the people and teams named on it
    (an admin is not named by being an admin), so the last of them must not be
    able to step down and strand it. Other audiences always keep their owners
    and admins, so stepping down from them is allowed."""
    if view.pack.audience != "restricted" or view.principal.superadmin:
        return
    me = view.principal.user_id
    other_person = await session.scalar(
        select(func.count()).select_from(KnowledgePackMember)
        .join(WorkspaceMember, and_(WorkspaceMember.workspace_id == view.ws_id,
                                    WorkspaceMember.user_id == KnowledgePackMember.user_id))
        .where(KnowledgePackMember.pack_id == view.pack.id,
               KnowledgePackMember.permission == MAINTAIN,
               KnowledgePackMember.user_id != me,
               WorkspaceMember.role.in_(sorted(PACK_MAINTAINER_ROLES))))
    other_team = await session.scalar(
        select(func.count()).select_from(KnowledgePackTeam)
        .where(KnowledgePackTeam.pack_id == view.pack.id,
               KnowledgePackTeam.permission == MAINTAIN))
    if not (other_person or other_team):
        raise LockOut()


async def remove_member(session: AsyncSession, view: PackView, user_id: str) -> None:
    require_maintainer(view)
    row = await session.get(KnowledgePackMember, (view.pack.id, (user_id or "")[:200]))
    if row is None:
        raise MemberNotFound()
    if row.user_id == view.principal.user_id:
        await _require_another_maintainer(session, view)
    await session.delete(row)
    await session.commit()


# ─── Versions ────────────────────────────────────────────────────────


async def add_version(session: AsyncSession, view: PackView, result: Any,
                      note: str = "") -> KnowledgePackVersion:
    """Store ``result`` (an ``IngestResult``) as the pack's next version and make
    it the current one; versions past the newest :data:`KEEP_VERSIONS` go."""
    from src.knowledge.ingest import version_sha

    require_maintainer(view)
    note = _check_text(note, MAX_NOTE, "note", multiline=True)
    pack = view.pack
    # Two uploads at once must not take one number; the pack row is the lock.
    await _lock_pack(session, pack)
    last = await session.scalar(select(func.max(KnowledgePackVersion.number))
                                .where(KnowledgePackVersion.pack_id == pack.id))
    files = list(result.files)
    version = KnowledgePackVersion(
        pack_id=pack.id, workspace_id=view.ws_id, number=(last or 0) + 1, note=note,
        created_by_id=view.principal.user_id, file_count=len(files),
        total_bytes=sum(f.size_bytes for f in files), sha=version_sha(files))
    session.add(version)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise Conflict("Another upload was in progress; try again.") from None
    session.add_all(KnowledgeItem(
        version_id=version.id, pack_id=pack.id, workspace_id=view.ws_id, path=f.path,
        kind=f.kind, skill=f.skill, name=f.name, description=f.description, content=f.content,
        sha=f.sha, size_bytes=f.size_bytes, repos=list(f.repos), tags=list(f.tags),
        terms=dict(f.terms)) for f in files)
    pack.current_version_id = version.id
    await session.flush()
    await _prune_versions(session, pack)
    await session.commit()
    await session.refresh(version)
    return version


async def _prune_versions(session: AsyncSession, pack: KnowledgePack) -> None:
    keep = (await session.scalars(
        select(KnowledgePackVersion.id).where(KnowledgePackVersion.pack_id == pack.id)
        .order_by(KnowledgePackVersion.number.desc()).limit(KEEP_VERSIONS))).all()
    keep_ids = set(keep)
    if pack.current_version_id:
        keep_ids.add(pack.current_version_id)
    old = [i for i in (await session.scalars(
        select(KnowledgePackVersion.id).where(KnowledgePackVersion.pack_id == pack.id))).all()
        if i not in keep_ids]
    if old:
        await session.execute(delete(KnowledgeItem).where(
            KnowledgeItem.pack_id == pack.id, KnowledgeItem.version_id.in_(old)))
        await session.execute(delete(KnowledgePackVersion).where(
            KnowledgePackVersion.pack_id == pack.id, KnowledgePackVersion.id.in_(old)))


async def _version(session: AsyncSession, view: PackView, number: int) -> KnowledgePackVersion:
    row = await session.scalar(select(KnowledgePackVersion).where(
        KnowledgePackVersion.pack_id == view.pack.id, KnowledgePackVersion.number == number))
    if row is None:
        raise VersionNotFound()
    return row


async def current_version(session: AsyncSession,
                          view: PackView) -> KnowledgePackVersion | None:
    """The version the pack serves now, or None before the first upload."""
    if not view.pack.current_version_id:
        return None
    return await session.scalar(select(KnowledgePackVersion).where(
        KnowledgePackVersion.id == view.pack.current_version_id,
        KnowledgePackVersion.pack_id == view.pack.id))


async def list_versions(session: AsyncSession, view: PackView) -> list[VersionInfo]:
    rows = (await session.scalars(
        select(KnowledgePackVersion).where(KnowledgePackVersion.pack_id == view.pack.id)
        .order_by(KnowledgePackVersion.number.desc()))).all()
    return [_version_info(r, view.pack.current_version_id) for r in rows]


async def _lock_pack(session: AsyncSession, pack: KnowledgePack) -> None:
    """Take the pack row's lock: uploads and rollbacks of ONE pack run one at a
    time, so a rollback cannot point at a version a concurrent upload is just
    pruning."""
    await session.execute(select(KnowledgePack.id).where(KnowledgePack.id == pack.id)
                          .with_for_update())


async def activate_version(session: AsyncSession, view: PackView, number: int) -> None:
    require_maintainer(view)
    await _lock_pack(session, view.pack)
    version = await _version(session, view, number)         # looked up under the lock
    view.pack.current_version_id = version.id
    await session.commit()


async def diff_versions(session: AsyncSession, view: PackView, number: int,
                        against: int | None = None) -> dict[str, list[str]]:
    """Paths added, removed and changed in version ``number`` relative to
    ``against`` (default: the version before it; nothing before it = all added)."""
    version = await _version(session, view, number)
    if against is None:
        previous = await session.scalar(
            select(func.max(KnowledgePackVersion.number)).where(
                KnowledgePackVersion.pack_id == view.pack.id,
                KnowledgePackVersion.number < number))
        other = await _version(session, view, previous) if previous else None
    else:
        other = await _version(session, view, against)

    async def shas(v: KnowledgePackVersion | None) -> dict[str, str]:
        if v is None:
            return {}
        rows = (await session.execute(select(KnowledgeItem.path, KnowledgeItem.sha).where(
            KnowledgeItem.version_id == v.id, KnowledgeItem.pack_id == view.pack.id))).tuples()
        return dict(rows.all())

    new, old = await shas(version), await shas(other)
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "changed": sorted(p for p in set(new) & set(old) if new[p] != old[p]),
    }


async def list_items(session: AsyncSession, view: PackView) -> list[ItemRef]:
    """Items of the pack's current version, by path."""
    if not view.pack.current_version_id:
        return []
    rows = (await session.execute(
        select(*_ITEM_COLUMNS).where(KnowledgeItem.version_id == view.pack.current_version_id,
                                     KnowledgeItem.pack_id == view.pack.id)
        .order_by(KnowledgeItem.path))).all()
    return [_item_ref(r) for r in rows]


async def get_item(session: AsyncSession, view: PackView,
                   item_id: str) -> tuple[KnowledgeItem, int]:
    """An item of ANY retained version of this pack, with its version number."""
    iid = valid_id(item_id)
    if iid is None:
        raise ItemNotFound()
    item = await session.scalar(select(KnowledgeItem).where(
        KnowledgeItem.id == iid, KnowledgeItem.pack_id == view.pack.id,
        KnowledgeItem.workspace_id == view.ws_id))
    if item is None:
        raise ItemNotFound()
    number = await session.scalar(select(KnowledgePackVersion.number).where(
        KnowledgePackVersion.id == item.version_id))
    return item, number or 0


_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


async def version_archive(session: AsyncSession, view: PackView,
                          number: int) -> tuple[bytes, str]:
    """A zip of version ``number`` (the same bytes for the same content) and the
    file name to offer."""
    version = await _version(session, view, number)
    rows = (await session.execute(select(KnowledgeItem.path, KnowledgeItem.content).where(
        KnowledgeItem.version_id == version.id, KnowledgeItem.pack_id == view.pack.id)
        .order_by(KnowledgeItem.path))).tuples().all()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in rows:
            info = zipfile.ZipInfo(path, _ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, content.encode())
    return buf.getvalue(), f"{view.pack.slug}-v{version.number}.zip"


async def usage(session: AsyncSession, view: PackView) -> dict[str, Any]:
    """What agents did with this pack in the last 30 days (for maintainers):
    :func:`src.knowledge.usage.pack_usage`, counts and times and no people."""
    from src.knowledge import usage as usage_mod

    require_maintainer(view)
    return await usage_mod.pack_usage(
        session, view.ws_id, view.pack.id, view.pack.current_version_id)


# ─── Reading the current content (REST and MCP share this shape) ─────


class Scanned(list):
    """A list that remembers whether the scan behind it stopped at
    :data:`MAX_RANKED_ITEMS`. It compares and iterates as the plain list."""

    truncated: bool = False


class Ranking(NamedTuple):
    """The best items for a query, from a scan of every visible current item."""

    hits: list[search_mod.Hit]                     # best first
    meta: dict[str, tuple[PackRef, int]]           # item id -> (pack, version number)
    pins: dict[str, str]                           # pack id -> version number, per pack read
    truncated: bool                                # the scan hit MAX_RANKED_ITEMS


NO_RANKING = Ranking([], {}, {}, False)

_LIGHT_COLUMNS = tuple(c for c in _ITEM_COLUMNS if c is not KnowledgeItem.terms)


class _Scan:
    """The items of some current versions, read a batch at a time in key order.

    A batch is a keyset page (pack, path, id), so memory is one batch however
    many items there are, and nothing is cut by pack. At most
    :data:`MAX_RANKED_ITEMS` are read; reaching that with rows left sets
    :attr:`truncated` and logs it."""

    def __init__(self, s: Session, version_ids: Sequence[str], *, with_terms: bool = True,
                 skill: str | None = None) -> None:
        self.s = s
        self.version_ids = list(version_ids)
        self.columns = _ITEM_COLUMNS if with_terms else _LIGHT_COLUMNS
        self.skill = skill
        self.truncated = False

    def _page(self, after: tuple | None, limit: int) -> list[Any]:
        query = select(*self.columns).where(KnowledgeItem.version_id.in_(self.version_ids))
        if self.skill is not None:
            query = query.where(KnowledgeItem.skill == self.skill)
        if after is not None:
            query = query.where(tuple_(KnowledgeItem.pack_id, KnowledgeItem.path,
                                       KnowledgeItem.id) > after)
        return list(self.s.execute(query.order_by(
            KnowledgeItem.pack_id, KnowledgeItem.path, KnowledgeItem.id).limit(limit)).all())

    def batches(self) -> Iterator[list[Any]]:
        if not self.version_ids:
            return
        seen, after = 0, None
        while True:
            room = MAX_RANKED_ITEMS - seen
            if room <= 0:
                if self._page(after, 1):
                    self.truncated = True
                    logger.warning("pack_index_capped cap=%d", MAX_RANKED_ITEMS)
                return
            rows = self._page(after, min(SCAN_BATCH, room))
            if not rows:
                return
            yield rows
            seen += len(rows)
            last = rows[-1]
            after = (last.pack_id, last.path, last.id)


def _current_of(s: Session, packs: Sequence[KnowledgePack]):
    """``(pack id -> current version id, version id -> number, pack id -> ref)``
    for the packs that have a current version."""
    live = [p for p in packs if p.current_version_id]
    current = {p.id: p.current_version_id for p in live}
    numbers: dict[str, int] = {}
    if current:
        numbers = dict(s.execute(select(
            KnowledgePackVersion.id, KnowledgePackVersion.number).where(
            KnowledgePackVersion.id.in_(list(current.values())))).tuples().all())
    return current, numbers, {p.id: _pack_ref(p) for p in live}


def _items_core(s: Session, packs: Sequence[KnowledgePack], *, with_terms: bool = True,
                skill: str | None = None) -> Scanned:
    """``[(pack, version_number, item)]`` for the current version of ``packs``."""
    current, numbers, refs = _current_of(s, packs)
    out = Scanned()
    scan = _Scan(s, list(current.values()), with_terms=with_terms, skill=skill)
    for rows in scan.batches():
        for row in rows:
            ref = refs.get(row.pack_id)
            if ref is not None:
                out.append((ref, numbers.get(row.version_id, 0), _item_ref(row)))
    out.truncated = scan.truncated
    return out


class _Candidate(NamedTuple):
    """What ranking keeps of an item that shares a token with the query."""

    position: int
    item_id: str
    pack_id: str
    kind: str
    path: str
    name: str
    length: float
    tfs: tuple[float, ...]


def _bm25(wanted: Sequence[str], c: _Candidate, n_docs: int, avg: float,
          containing: Sequence[int]) -> float:
    """The score :func:`src.knowledge.search.rank` gives an item (same formula,
    same order of operations), from the corpus figures gathered in one pass."""
    score = 0.0
    for i, _term in enumerate(wanted):
        tf = c.tfs[i]
        if not tf:
            continue
        idf = math.log(1.0 + (n_docs - containing[i] + 0.5) / (containing[i] + 0.5))
        score += (idf * (tf * (search_mod.K1 + 1.0))
                  / (tf + search_mod.K1 * (1.0 - search_mod.B + search_mod.B * c.length / avg)))
    return score


def _rank_core(s: Session, packs: Sequence[KnowledgePack], query: str, limit: int,
               per_kind: Mapping[str, int] | None) -> Ranking:
    """Rank every current item of ``packs`` against ``query``, a batch at a time.

    The result equals ``search.rank`` over all the items (BM25 needs the whole
    corpus for its idf and average length, so those are counted while the items
    stream past and only the figures of items that match are kept). What is
    returned is bounded: the best ``limit`` items and, for each kind in
    ``per_kind``, the best ``n`` of that kind (the brief fills a section per
    kind), loaded by id at the end."""
    wanted = sorted(set(search_mod.tokenize(query)))
    current, numbers, refs = _current_of(s, packs)
    if not wanted or not current or (limit <= 0 and not per_kind):
        return NO_RANKING
    scan = _Scan(s, list(current.values()))
    n_docs, total_length, position = 0, 0.0, 0
    pins: dict[str, str] = {}
    matched: list[_Candidate] = []
    for rows in scan.batches():
        for row in rows:
            doc = search_mod.document(row)
            n_docs += 1
            total_length += doc.length
            pins.setdefault(row.pack_id, str(numbers.get(row.version_id, 0)))
            tfs = tuple(doc.tf.get(term, 0.0) for term in wanted)
            if any(tfs):
                matched.append(_Candidate(position, row.id, row.pack_id, row.kind, row.path,
                                          row.name, doc.length, tfs))
            position += 1
    avg = ((total_length / n_docs) or 1.0) if n_docs else 1.0
    containing = [sum(1 for c in matched if c.tfs[i]) for i in range(len(wanted))]
    scored = [(score, c) for c in matched
              if (score := _bm25(wanted, c, n_docs, avg, containing)) > 0.0]

    def order(pair: tuple[float, _Candidate]) -> tuple:
        return (-pair[0], pair[1].path, pair[1].name, pair[1].position)

    chosen = {c.item_id: (score, c) for score, c in heapq.nsmallest(max(limit, 0), scored, key=order)}
    for kind, count in (per_kind or {}).items():
        for score, c in heapq.nsmallest(
                count, (p for p in scored if p[1].kind == kind), key=order):
            chosen[c.item_id] = (score, c)
    ordered = sorted(chosen.values(), key=order)
    if not ordered:
        return Ranking([], {}, pins, scan.truncated)
    rows = {r.id: r for r in s.execute(select(*_ITEM_COLUMNS).where(
        KnowledgeItem.id.in_([c.item_id for _score, c in ordered]),
        KnowledgeItem.version_id.in_(list(current.values())))).all()}
    hits, meta = [], {}
    for score, c in ordered:
        row, ref = rows.get(c.item_id), refs.get(c.pack_id)
        if row is None or ref is None:
            continue
        item = _item_ref(row)
        hits.append(search_mod.Hit(score, item))
        meta[item.id] = (ref, numbers.get(row.version_id, 0))
    return Ranking(hits, meta, pins, scan.truncated)


async def visible_current_items(session: AsyncSession, principal: PackPrincipal,
                                ws_id: str) -> Scanned:
    """``[(pack, version_number, item)]`` of the CURRENT version of every pack the
    principal may see (read in batches; ``.truncated`` says whether the scan
    stopped at the safety cap)."""
    views = await list_views(session, principal, ws_id)
    return await session.run_sync(_items_core, [v.pack for v in views])


async def rank_current(session: AsyncSession, principal: PackPrincipal, ws_id: str, query: str,
                       *, limit: int = 20, per_kind: Mapping[str, int] | None = None) -> Ranking:
    """The best ``limit`` current items of visible packs for ``query`` (and the
    best of each kind in ``per_kind``), best first — see :func:`_rank_core`."""
    views = await list_views(session, principal, ws_id)
    return await session.run_sync(_rank_core, [v.pack for v in views], query, limit, per_kind)


async def search(session: AsyncSession, principal: PackPrincipal, ws_id: str, query: str,
                 limit: int = 20) -> Scanned:
    """Ranked items of the current versions of visible packs, each with the
    paragraph that matched (``.truncated``: the scan stopped at the safety cap)."""
    ranking = await rank_current(session, principal, ws_id, query, limit=limit)
    out = Scanned()
    out.truncated = ranking.truncated
    if not ranking.hits:
        return out
    texts = dict((await session.execute(select(KnowledgeItem.id, KnowledgeItem.content).where(
        KnowledgeItem.id.in_([h.item.id for h in ranking.hits])))).tuples().all())
    words = search_mod.tokenize(query)
    out.extend(SearchHit(ranking.meta[h.item.id][0], ranking.meta[h.item.id][1], h.item, h.score,
                         search_mod.snippet(texts.get(h.item.id, ""), words))
               for h in ranking.hits)
    return out


async def item_contents(session: AsyncSession, principal: PackPrincipal, ws_id: str,
                        item_ids: Sequence[str]) -> dict[str, str]:
    """Text of the given items — only those inside packs the principal may see."""
    ids = [i for i in (valid_id(x) for x in item_ids) if i]
    if not ids:
        return {}
    visible = sorted(v.pack.id for v in await list_views(session, principal, ws_id))
    if not visible:
        return {}
    rows = (await session.execute(select(KnowledgeItem.id, KnowledgeItem.content).where(
        KnowledgeItem.id.in_(ids), KnowledgeItem.workspace_id == ws_id,
        KnowledgeItem.pack_id.in_(visible)))).tuples().all()
    return dict(rows)


# ─── Invites ─────────────────────────────────────────────────────────

MAX_TOKEN_CHARS = 200


def _invite_open(inv: KnowledgePackInvite) -> bool:
    expires = _aware(inv.expires_at)
    return (inv.revoked_at is None and expires is not None and expires > _now()
            and inv.used_count < inv.max_uses)


async def list_invites(session: AsyncSession, view: PackView) -> list[KnowledgePackInvite]:
    require_maintainer(view)
    rows = (await session.scalars(select(KnowledgePackInvite).where(
        KnowledgePackInvite.pack_id == view.pack.id))).all()
    return sorted(rows, key=lambda r: (r.created_at is None, r.created_at), reverse=True)


async def create_invite(session: AsyncSession, view: PackView, *, expires_in_days: int = 7,
                        max_uses: int = 25) -> tuple[KnowledgePackInvite, str]:
    """A join link for the pack. The raw token is returned HERE ONLY; the row
    keeps its sha256."""
    require_maintainer(view)
    if not 1 <= expires_in_days <= 30 or not 1 <= max_uses <= 500:
        raise BadInput("expires_in_days must be 1-30 and max_uses 1-500.")
    live = [i for i in await list_invites(session, view) if _invite_open(i)]
    if len(live) >= MAX_ACTIVE_INVITES:
        raise LimitReached(f"A pack holds at most {MAX_ACTIVE_INVITES} open invite links.")
    token = secrets.token_urlsafe(32)
    row = KnowledgePackInvite(
        pack_id=view.pack.id, workspace_id=view.ws_id, token_hash=_hash(token),
        permission="read", created_by_id=view.principal.user_id,
        expires_at=_now() + timedelta(days=expires_in_days), max_uses=max_uses)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, token


async def revoke_invite(session: AsyncSession, view: PackView, invite_id: str) -> None:
    require_maintainer(view)
    iid = valid_id(invite_id)
    row = await session.get(KnowledgePackInvite, iid) if iid else None
    if row is None or row.pack_id != view.pack.id:
        raise InviteNotFound()
    if row.revoked_at is None:
        row.revoked_at = _now()
        await session.commit()


async def _open_invite(session: AsyncSession, user: Any,
                       token: str) -> tuple[KnowledgePackInvite, KnowledgePack]:
    """The invite and its pack, when the token is good AND the person holds a
    role in the invite's workspace. Every other case is the same refusal."""
    token = (token or "").strip()
    if not token or len(token) > MAX_TOKEN_CHARS or user is None:
        raise InviteNotFound()
    inv = await session.scalar(select(KnowledgePackInvite).where(
        KnowledgePackInvite.token_hash == _hash(token)))
    if inv is None or not _invite_open(inv):
        raise InviteNotFound()
    pack = await session.get(KnowledgePack, inv.pack_id)
    if pack is None or pack.workspace_id != inv.workspace_id:
        raise InviteNotFound()
    member = await session.get(WorkspaceMember, (inv.workspace_id, user.id))
    if member is None or member.role not in VALID_WORKSPACE_ROLES:
        raise InviteNotFound()
    return inv, pack


class InvitePreview(NamedTuple):
    pack_title: str
    pack_description: str
    workspace_id: str
    already_member: bool


async def preview_invite(session: AsyncSession, user: Any, token: str) -> InvitePreview:
    inv, pack = await _open_invite(session, user, token)
    named = await session.get(KnowledgePackMember, (pack.id, user.id)) is not None
    return InvitePreview(pack.title, pack.description or "", inv.workspace_id, named)


async def redeem_invite(session: AsyncSession, user: Any,
                        token: str) -> tuple[KnowledgePack, bool]:
    """Join the pack. Returns (pack, joined_now). Already named (read or
    maintain) changes nothing — a link never lowers a maintainer — and does not
    use up the link."""
    inv, pack = await _open_invite(session, user, token)
    if await session.get(KnowledgePackMember, (pack.id, user.id)) is not None:
        return pack, False
    count = await session.scalar(select(func.count()).select_from(KnowledgePackMember)
                                 .where(KnowledgePackMember.pack_id == pack.id))
    if (count or 0) >= MAX_MEMBERS:
        raise LimitReached(f"A pack names at most {MAX_MEMBERS} people.")
    taken = await session.execute(
        update(KnowledgePackInvite)
        .where(KnowledgePackInvite.id == inv.id, KnowledgePackInvite.revoked_at.is_(None),
               KnowledgePackInvite.used_count < KnowledgePackInvite.max_uses)
        .values(used_count=KnowledgePackInvite.used_count + 1))
    if taken.rowcount != 1:
        await session.rollback()
        raise InviteNotFound()
    session.add(KnowledgePackMember(pack_id=pack.id, user_id=user.id, permission=inv.permission,
                                    invite_id=inv.id, granted_by_id=inv.created_by_id))
    try:
        await session.commit()
    except IntegrityError:               # joined twice at once: the same outcome
        await session.rollback()
        return pack, False
    return pack, True


# ─── Publish tokens (cpk_) ───────────────────────────────────────────

#: What a pack publish token starts with. Nothing else in the product accepts it
#: and the gate accepts nothing else.
TOKEN_PREFIX = "cpk_"
#: The one sentence for every way a publish token can fail — a token that never
#: existed, was revoked, ran out, or whose maker lost the right to maintain.
TOKEN_INVALID = "invalid or expired pack token"
#: Longest token the gate reads (a real one is 47 characters).
MAX_PUBLISH_TOKEN_CHARS = 200


@dataclass
class PublishAuth:
    """A publish token that passed: the pack, seen as its maker sees it NOW."""

    token: KnowledgePackToken
    user: Any
    view: PackView


def _token_live(row: KnowledgePackToken) -> bool:
    expires = _aware(row.expires_at)
    return row.revoked_at is None and expires is not None and expires > _now()


async def list_tokens(session: AsyncSession, view: PackView) -> list[KnowledgePackToken]:
    require_maintainer(view)
    rows = (await session.scalars(select(KnowledgePackToken).where(
        KnowledgePackToken.pack_id == view.pack.id))).all()
    return sorted(rows, key=lambda r: (r.created_at is None, r.created_at), reverse=True)


async def create_token(session: AsyncSession, view: PackView, *, label: str,
                       expires_in_days: int = DEFAULT_TOKEN_DAYS
                       ) -> tuple[KnowledgePackToken, str]:
    """A publish token for the pack, made by the maintainer asking. The raw token
    is returned HERE ONLY; the row keeps its sha256."""
    require_maintainer(view)
    label = _check_text(label, MAX_LABEL, "label", required=True)
    if not 1 <= expires_in_days <= MAX_TOKEN_DAYS:
        raise BadInput(f"expires_in_days must be 1-{MAX_TOKEN_DAYS}.")
    live = [t for t in await list_tokens(session, view) if _token_live(t)]
    if len(live) >= MAX_ACTIVE_TOKENS:
        raise LimitReached(f"A pack holds at most {MAX_ACTIVE_TOKENS} active publish tokens.")
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = KnowledgePackToken(
        pack_id=view.pack.id, workspace_id=view.ws_id, label=label, token_hash=_hash(raw),
        created_by_id=view.principal.user_id,
        expires_at=_now() + timedelta(days=expires_in_days))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, raw


async def revoke_token(session: AsyncSession, view: PackView, token_id: str) -> KnowledgePackToken:
    require_maintainer(view)
    tid = valid_id(token_id)
    row = await session.get(KnowledgePackToken, tid) if tid else None
    if row is None or row.pack_id != view.pack.id:
        raise TokenNotFound()
    if row.revoked_at is None:
        row.revoked_at = _now()
        await session.commit()
    return row


async def authenticate_token(session: AsyncSession, raw: str | None) -> PublishAuth | None:
    """The pack a publish token opens, or None — for EVERY way it can fail.

    Asked on every call, with nothing cached: the row must exist and be neither
    revoked nor expired, its maker must still exist and be active, and the maker
    must be able to MAINTAIN the pack right now (a fresh principal: a team they
    were removed from, an audience that changed, a workspace they left, an
    account that was switched off all end the token on its next use). The caller
    cannot tell which of those it was, and neither can a timing probe tell a
    token that never existed from one that lapsed beyond what a lookup costs.
    """
    from src.knowledge.visibility import principal_for
    from src.users.store import get_user_store

    token = (raw or "").strip()
    if (not token.startswith(TOKEN_PREFIX) or len(token) > MAX_PUBLISH_TOKEN_CHARS
            or not token.isascii()):
        return None
    row = await session.scalar(select(KnowledgePackToken).where(
        KnowledgePackToken.token_hash == _hash(token))
        .execution_options(populate_existing=True))
    if row is None or not _token_live(row):
        return None
    user = get_user_store().get_by_id(row.created_by_id)
    if user is None or not getattr(user, "is_active", True):
        return None
    principal = await principal_for(session, user, row.workspace_id)
    try:
        view = await get_pack(session, principal, row.workspace_id, row.pack_id)
    except PackNotAccessible:
        return None
    if not view.can_maintain:
        return None
    row.last_used_at = _now()
    await session.commit()
    return PublishAuth(row, user, view)


# ─── Cleanup (add to the caller's transaction; never commit) ─────────


def _workspace_pack_deletes(pack_ids: Sequence[str], ws_id: str) -> list[Any]:
    out = _pack_row_deletes(pack_ids)
    out += [delete(model).where(model.workspace_id == ws_id)
            for model in (KnowledgeItem, KnowledgePackVersion, KnowledgePackInvite,
                          KnowledgePackToken, KnowledgePack)]
    return out


def _erase_user_statements(user_id: str) -> list[Any]:
    # A publish token is its maker's: with the person gone it opens nothing. The
    # invite links they made are revoked BEFORE the ids that name them are cleared
    # below (after that nobody could tell whose they were).
    out: list[Any] = [delete(KnowledgePackMember).where(KnowledgePackMember.user_id == user_id),
                      delete(KnowledgePackToken).where(
                          KnowledgePackToken.created_by_id == user_id),
                      update(KnowledgePackInvite).where(
                          KnowledgePackInvite.created_by_id == user_id,
                          KnowledgePackInvite.revoked_at.is_(None)).values(revoked_at=_now())]
    for model, column in ((KnowledgePack, "created_by_id"), (KnowledgePackVersion, "created_by_id"),
                          (KnowledgePackInvite, "created_by_id"),
                          (KnowledgePackTeam, "granted_by_id"),
                          (KnowledgePackMember, "granted_by_id")):
        col = getattr(model, column)
        out.append(update(model).where(col == user_id).values({column: None}))
    return out


class Orphaned(NamedTuple):
    """A restricted pack that lost its last person or team able to maintain it."""

    pack_id: str
    workspace_id: str


def _leaver_statements(user_id: str, ws_ids: Sequence[str]) -> list[Any]:
    """Everything a person holds on the packs of ``ws_ids``, ended: the places
    they are named on, and the publish tokens and invite links they made (revoked,
    so the rows stay as a record). Nothing is left to come back to life if they
    are added to the workspace again, and no link they handed out keeps adding
    people to a pack in their name."""
    in_workspaces = select(KnowledgePack.id).where(KnowledgePack.workspace_id.in_(list(ws_ids)))
    return [
        delete(KnowledgePackMember).where(
            KnowledgePackMember.user_id == user_id,
            KnowledgePackMember.pack_id.in_(in_workspaces)),
        update(KnowledgePackToken).where(
            KnowledgePackToken.created_by_id == user_id,
            KnowledgePackToken.workspace_id.in_(list(ws_ids)),
            KnowledgePackToken.revoked_at.is_(None)).values(revoked_at=_now()),
        update(KnowledgePackInvite).where(
            KnowledgePackInvite.created_by_id == user_id,
            KnowledgePackInvite.workspace_id.in_(list(ws_ids)),
            KnowledgePackInvite.revoked_at.is_(None)).values(revoked_at=_now()),
    ]


def _maintained_by_query(user_id: str, ws_ids: Sequence[str] | None):
    """The restricted packs (of ``ws_ids``, or of any workspace) this person
    maintains by name — the only ones whose last maintainer they could be."""
    query = (select(KnowledgePack.id, KnowledgePack.workspace_id)
             .join(KnowledgePackMember, KnowledgePackMember.pack_id == KnowledgePack.id)
             .where(KnowledgePackMember.user_id == user_id,
                    KnowledgePackMember.permission == MAINTAIN,
                    KnowledgePack.audience == "restricted"))
    if ws_ids is not None:
        query = query.where(KnowledgePack.workspace_id.in_(list(ws_ids)))
    return query


def _still_maintained_query(pack_ids: Sequence[str]):
    """The ids among ``pack_ids`` that a current member of the workspace (by
    name; a viewer maintains nothing) or a team of the workspace (by grant) can
    still maintain."""
    by_person = (select(KnowledgePackMember.pack_id)
                 .join(KnowledgePack, KnowledgePack.id == KnowledgePackMember.pack_id)
                 .join(WorkspaceMember, and_(
                     WorkspaceMember.workspace_id == KnowledgePack.workspace_id,
                     WorkspaceMember.user_id == KnowledgePackMember.user_id))
                 .where(KnowledgePackMember.pack_id.in_(list(pack_ids)),
                        KnowledgePackMember.permission == MAINTAIN,
                        WorkspaceMember.role.in_(sorted(PACK_MAINTAINER_ROLES))))
    by_team = (select(KnowledgePackTeam.pack_id)
               .join(KnowledgePack, KnowledgePack.id == KnowledgePackTeam.pack_id)
               .join(Team, and_(Team.id == KnowledgePackTeam.team_id,
                                Team.workspace_id == KnowledgePack.workspace_id))
               .where(KnowledgePackTeam.pack_id.in_(list(pack_ids)),
                      KnowledgePackTeam.permission == MAINTAIN))
    return by_person.union(by_team)


class Footprint(NamedTuple):
    """What a person holds on packs that is about to end (ids only, read BEFORE
    the statements that end it): ``(pack_id, workspace_id)`` for each name, and
    ``(pack_id, workspace_id, id)`` for each live publish token and invite link."""

    names: list[tuple[str, str]]
    tokens: list[tuple[str, str, str]]
    invites: list[tuple[str, str, str]]


def _footprint_queries(user_id: str, ws_ids: Sequence[str] | None) -> tuple[Any, Any, Any]:
    names = (select(KnowledgePackMember.pack_id, KnowledgePack.workspace_id)
             .join(KnowledgePack, KnowledgePack.id == KnowledgePackMember.pack_id)
             .where(KnowledgePackMember.user_id == user_id))
    tokens = select(KnowledgePackToken.pack_id, KnowledgePackToken.workspace_id,
                    KnowledgePackToken.id).where(
        KnowledgePackToken.created_by_id == user_id, KnowledgePackToken.revoked_at.is_(None))
    invites = select(KnowledgePackInvite.pack_id, KnowledgePackInvite.workspace_id,
                     KnowledgePackInvite.id).where(
        KnowledgePackInvite.created_by_id == user_id, KnowledgePackInvite.revoked_at.is_(None))
    if ws_ids is not None:
        names = names.where(KnowledgePack.workspace_id.in_(list(ws_ids)))
        tokens = tokens.where(KnowledgePackToken.workspace_id.in_(list(ws_ids)))
        invites = invites.where(KnowledgePackInvite.workspace_id.in_(list(ws_ids)))
    return names, tokens, invites


def _footprint(rows: tuple[Any, Any, Any]) -> Footprint:
    names, tokens, invites = rows
    return Footprint([(r[0], r[1]) for r in names], [(r[0], r[1], r[2]) for r in tokens],
                     [(r[0], r[1], r[2]) for r in invites])


async def _footprint_of(session: AsyncSession, user_id: str,
                        ws_ids: Sequence[str] | None) -> Footprint:
    names, tokens, invites = _footprint_queries(user_id, ws_ids)
    return _footprint(((await session.execute(names)).all(),
                       (await session.execute(tokens)).all(),
                       (await session.execute(invites)).all()))


def _footprint_of_sync(session: Session, user_id: str, ws_ids: Sequence[str] | None) -> Footprint:
    names, tokens, invites = _footprint_queries(user_id, ws_ids)
    return _footprint((session.execute(names).all(), session.execute(tokens).all(),
                       session.execute(invites).all()))


def _announce_release(session: Any, footprint: Footprint, *, reason: str, user_id: str | None,
                      actor: str | None, actor_id: str | None) -> None:
    """Write the audit rows of what :func:`release_person` / :func:`erase_user`
    ended - a name taken off a pack (``codebase.member_removed``), a publish token
    (``codebase.token_revoked``) and an invite link (``codebase.invite_revoked``) -
    once the surrounding transaction COMMITS, the same rows the pack routes write
    when a maintainer does it by hand. The target is the pack; the detail is ids
    and the reason, never a name or a label. ``user_id`` is left out for an erased
    account."""
    if not (footprint.names or footprint.tokens or footprint.invites):
        return
    who = {"user_id": user_id} if user_id else {}

    def record(_session: Any) -> None:
        from src.security.audit import record_action

        def row(action: str, pack_id: str, ws_id: str, **detail: str) -> None:
            record_action(action=f"codebase.{action}", actor=actor, actor_id=actor_id,
                          workspace_id=ws_id, target=pack_id,
                          detail={**detail, "reason": reason})

        for pack_id, ws_id in footprint.names:
            row("member_removed", pack_id, ws_id, **who)
        for pack_id, ws_id, token_id in footprint.tokens:
            row("token_revoked", pack_id, ws_id, token_id=token_id)
        for pack_id, ws_id, invite_id in footprint.invites:
            row("invite_revoked", pack_id, ws_id, invite_id=invite_id)

    target = getattr(session, "sync_session", session)
    event.listen(target, "after_commit", record, once=True)


def _announce_orphans(session: Any, orphans: Sequence[Orphaned], *, reason: str,
                      actor: str | None, actor_id: str | None) -> None:
    """Write one ``codebase.pack_orphaned`` audit row per pack once the
    surrounding transaction COMMITS (the caller owns the commit, and a row for a
    change that was rolled back would be a false alarm). The target is the pack,
    so the audit page shows it only to people who may see the pack."""
    if not orphans:
        return

    def record(_session: Any) -> None:
        from src.security.audit import record_action

        for orphan in orphans:
            record_action(
                action="codebase.pack_orphaned", actor=actor, actor_id=actor_id,
                workspace_id=orphan.workspace_id, target=orphan.pack_id,
                detail={"reason": reason})

    target = getattr(session, "sync_session", session)
    event.listen(target, "after_commit", record, once=True)


def _orphans_after(candidates: Sequence[Any], still: Iterable[str]) -> list[Orphaned]:
    kept = set(still)
    return [Orphaned(row.id, row.workspace_id) for row in candidates if row.id not in kept]


async def release_person(session: AsyncSession, user_id: str, ws_ids: Sequence[str], *,
                         actor: str | None = None, actor_id: str | None = None) -> list[Orphaned]:
    """A person left (or was removed from) the workspaces ``ws_ids``: end what
    they hold on those workspaces' packs, in the caller's transaction.

    Their names on packs go and the publish tokens and invite links they made
    are revoked, so adding them back later — in any role — restores nothing. A restricted pack that was
    maintained only by this person is NOT handed to anyone (that would show it
    to somebody it was never shared with): it stays as it is, visible to the
    superadmin alone, and a ``codebase.pack_orphaned`` audit row is written once
    the transaction commits. The superadmin can name a new maintainer in the
    ordinary Sharing tab. Returns the packs left without a maintainer."""
    if not ws_ids:
        return []
    candidates = (await session.execute(_maintained_by_query(user_id, ws_ids))).all()
    footprint = await _footprint_of(session, user_id, ws_ids)
    for statement in _leaver_statements(user_id, ws_ids):
        await session.execute(statement)
    orphans: list[Orphaned] = []
    if candidates:
        still = (await session.scalars(
            _still_maintained_query([c.id for c in candidates]))).all()
        orphans = _orphans_after(candidates, still)
    _announce_release(session, footprint, reason="member_left", user_id=user_id,
                      actor=actor, actor_id=actor_id)
    _announce_orphans(session, orphans, reason="member_left", actor=actor, actor_id=actor_id)
    return orphans


async def delete_workspace_packs(session: AsyncSession, ws_id: str) -> None:
    """Every pack of a workspace and all it owns."""
    ids = (await session.scalars(select(KnowledgePack.id).where(
        KnowledgePack.workspace_id == ws_id))).all()
    for statement in _workspace_pack_deletes(ids, ws_id):
        await session.execute(statement)


def delete_workspace_packs_sync(session: Session, ws_id: str) -> None:
    """:func:`delete_workspace_packs` for a synchronous session (account deletion)."""
    ids = session.scalars(select(KnowledgePack.id).where(KnowledgePack.workspace_id == ws_id)).all()
    for statement in _workspace_pack_deletes(ids, ws_id):
        session.execute(statement)


async def delete_team_grants(session: AsyncSession, team_id: str) -> None:
    await session.execute(delete(KnowledgePackTeam).where(KnowledgePackTeam.team_id == team_id))


async def erase_user(session: AsyncSession, user_id: str) -> list[Orphaned]:
    """Forget a person: their names on packs go, and so do the ids that recorded
    who made or granted what. A restricted pack they alone maintained is left as
    it is and reported, as in :func:`release_person`."""
    candidates = (await session.execute(_maintained_by_query(user_id, None))).all()
    footprint = await _footprint_of(session, user_id, None)
    for statement in _erase_user_statements(user_id):
        await session.execute(statement)
    orphans: list[Orphaned] = []
    if candidates:
        still = (await session.scalars(
            _still_maintained_query([c.id for c in candidates]))).all()
        orphans = _orphans_after(candidates, still)
    _announce_release(session, footprint, reason="account_erased", user_id=None,
                      actor=None, actor_id=None)
    _announce_orphans(session, orphans, reason="account_erased", actor=None, actor_id=None)
    return orphans


def erase_user_sync(session: Session, user_id: str) -> list[Orphaned]:
    """:func:`erase_user` for a synchronous session (account deletion)."""
    candidates = session.execute(_maintained_by_query(user_id, None)).all()
    footprint = _footprint_of_sync(session, user_id, None)
    for statement in _erase_user_statements(user_id):
        session.execute(statement)
    orphans: list[Orphaned] = []
    if candidates:
        still = session.scalars(_still_maintained_query([c.id for c in candidates])).all()
        orphans = _orphans_after(candidates, still)
    _announce_release(session, footprint, reason="account_erased", user_id=None,
                      actor=None, actor_id=None)
    _announce_orphans(session, orphans, reason="account_erased", actor=None, actor_id=None)
    return orphans


async def memberships_of(session: AsyncSession, user_id: str) -> list[dict[str, Any]]:
    """What a person's data export lists: the packs they are named on."""
    rows = (await session.execute(
        select(KnowledgePackMember.pack_id, KnowledgePackMember.permission,
               KnowledgePack.workspace_id, KnowledgePack.slug)
        .join(KnowledgePack, KnowledgePack.id == KnowledgePackMember.pack_id)
        .where(KnowledgePackMember.user_id == user_id))).all()
    return [{"pack_id": r.pack_id, "workspace_id": r.workspace_id, "slug": r.slug,
             "permission": r.permission} for r in rows]


# ─── Synchronous twins for the MCP tools ─────────────────────────────


def _sync_session() -> Session:
    from src.access.resolver import _sync_engine

    return Session(_sync_engine())


def _visible_packs_sync(s: Session, principal: PackPrincipal, ws_id: str) -> list[KnowledgePack]:
    rows = list(s.scalars(visible_packs_query(principal, ws_id)).all())
    if not rows:
        return []
    ids = [p.id for p in rows]
    grants, members = _group(s.scalars(_grant_query(ids, ws_id)).all(),
                             s.scalars(_member_query(ids, principal.user_id)).all())
    return [v.pack for v in _views(principal, ws_id, rows, grants, members)]


def visible_pack_ids_sync(principal: PackPrincipal, ws_id: str) -> frozenset[str]:
    """The ids of the packs of ``ws_id`` the principal may see right now. A
    principal that sees nothing, and any failure, give an empty set (the audit
    page asks this per record, and a pack that is gone is not in it)."""
    try:
        with _sync_session() as s:
            return frozenset(p.id for p in _visible_packs_sync(s, principal, ws_id))
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("pack_read_failed err=%s", type(exc).__name__)
        return frozenset()


def visible_current_items_sync(principal: PackPrincipal, ws_id: str, *,
                               slug: str | None = None, skill: str | None = None,
                               with_terms: bool = True) -> Scanned:
    """``[(pack, version_number, item)]`` for the CURRENT version of every pack
    the principal may see (only the pack ``slug``, only the items of ``skill``,
    when given; ``with_terms=False`` leaves out the ranking terms). A principal
    that sees nothing, and any failure, give ``[]``; ``.truncated`` says whether
    the scan stopped at the safety cap."""
    try:
        with _sync_session() as s:
            packs = [p for p in _visible_packs_sync(s, principal, ws_id)
                     if slug is None or p.slug == slug]
            return _items_core(s, packs, with_terms=with_terms, skill=skill)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("pack_read_failed err=%s", type(exc).__name__)
        return Scanned()


def rank_current_sync(principal: PackPrincipal, ws_id: str, query: str, *, limit: int = 20,
                      per_kind: Mapping[str, int] | None = None) -> Ranking:
    """:func:`rank_current` for the MCP tools. Any failure ranks nothing."""
    try:
        with _sync_session() as s:
            return _rank_core(s, _visible_packs_sync(s, principal, ws_id), query, limit, per_kind)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("pack_read_failed err=%s", type(exc).__name__)
        return NO_RANKING


def pack_items_sync(principal: PackPrincipal, ws_id: str,
                    slug: str) -> list[tuple[PackRef, int, ItemRef]]:
    """The current items of ONE visible pack, by its slug (``[]`` when it is
    missing or hidden — the two cannot be told apart)."""
    return visible_current_items_sync(principal, ws_id, slug=slug, with_terms=False)


def find_skill_sync(principal: PackPrincipal, ws_id: str, name: str) -> SkillBundle | None:
    """A visible skill by ``pack/skill``, or by bare ``skill`` when exactly one
    visible pack has it. None when missing, hidden or ambiguous."""
    name = (name or "").strip()
    slug, _, skill = name.partition("/") if "/" in name else ("", "", name)
    if not skill or ("/" in name and not slug):
        return None
    items = [t for t in visible_current_items_sync(
        principal, ws_id, slug=slug or None, skill=skill, with_terms=False)
        if t[2].skill == skill and (not slug or t[0].slug == slug)]
    heads = [t for t in items if t[2].kind == "skill"]
    if len({t[0].id for t in heads}) != 1:
        return None
    pack, number, head = heads[0]
    files = sorted((t[2] for t in items if t[0].id == pack.id), key=lambda i: i.path)
    texts = item_contents_sync(principal, ws_id, [head.id])
    if head.id not in texts:
        return None
    directory = head.path.rsplit("/", 1)[0] if "/" in head.path else ""
    return SkillBundle(pack, number, head, texts[head.id], files, directory)


def item_contents_sync(principal: PackPrincipal, ws_id: str,
                       item_ids: Sequence[str]) -> dict[str, str]:
    """Text of the given items — only those inside packs the principal may see."""
    ids = [i for i in (valid_id(x) for x in item_ids) if i]
    if not ids:
        return {}
    try:
        with _sync_session() as s:
            visible = {p.id for p in _visible_packs_sync(s, principal, ws_id)}
            if not visible:
                return {}
            rows = s.execute(select(KnowledgeItem.id, KnowledgeItem.content).where(
                KnowledgeItem.id.in_(ids), KnowledgeItem.workspace_id == ws_id,
                KnowledgeItem.pack_id.in_(sorted(visible)))).tuples().all()
            return dict(rows)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("pack_read_failed err=%s", type(exc).__name__)
        return {}


__all__ = [
    "DEFAULT_TOKEN_DAYS",
    "KEEP_VERSIONS",
    "MAX_ACTIVE_TOKENS",
    "MAX_LABEL",
    "MAX_TOKEN_DAYS",
    "NO_RANKING",
    "TOKEN_INVALID",
    "TOKEN_PREFIX",
    "BadInput",
    "Conflict",
    "InviteNotFound",
    "InvitePreview",
    "ItemNotFound",
    "ItemRef",
    "LimitReached",
    "LockOut",
    "MemberNotFound",
    "NotAuthor",
    "NotMaintainer",
    "Orphaned",
    "PackError",
    "PackInfo",
    "PackRef",
    "PackView",
    "PersonNotFound",
    "PublishAuth",
    "Ranking",
    "Scanned",
    "SearchHit",
    "SkillBundle",
    "SlugTaken",
    "TeamGrantInfo",
    "TeamNotFound",
    "TokenNotFound",
    "VersionInfo",
    "VersionNotFound",
    "activate_version",
    "add_member",
    "add_version",
    "authenticate_token",
    "create_invite",
    "create_pack",
    "create_token",
    "current_version",
    "delete_pack",
    "delete_team_grants",
    "delete_workspace_packs",
    "describe",
    "diff_versions",
    "erase_user",
    "find_skill_sync",
    "get_item",
    "get_pack",
    "item_contents",
    "item_contents_sync",
    "list_invites",
    "list_items",
    "list_members",
    "list_tokens",
    "list_versions",
    "list_views",
    "memberships_of",
    "pack_items_sync",
    "preview_invite",
    "rank_current",
    "rank_current_sync",
    "redeem_invite",
    "release_person",
    "remove_member",
    "require_maintainer",
    "revoke_invite",
    "revoke_token",
    "search",
    "set_teams",
    "update_pack",
    "usage",
    "valid_id",
    "version_archive",
    "visible_current_items",
    "visible_current_items_sync",
    "visible_pack_ids_sync",
]
