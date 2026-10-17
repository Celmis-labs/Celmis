"""Putting a folder or a zip into a pack from the command line.

The operator who runs ``analyzer codebase import`` has the database already, so
this is the one door that does not go through a signed-in person — it acts as
the superadmin unless ``--as`` names a person, in which case that person's own
standing decides (they must be allowed to create packs, or to maintain the pack
that exists). Everything else is the store: the same limits, the same checks of
the upload, the same version rules. What it prints is a count, never a file's
text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import KnowledgePack, Team, Workspace
from src.knowledge import store
from src.knowledge.visibility import PackNotAccessible, PackPrincipal, can_create
from src.users.roles import PACK_PERMISSIONS

#: The audit actor and the id recorded as author for work done by the operator.
CLI_ACTOR = "cli"

#: Acts as the superadmin: the person at the keyboard can read the database anyway.
OPERATOR = PackPrincipal(CLI_ACTOR, True, False, None, frozenset())


class ImportRefusedError(Exception):
    """A refusal the command line prints as one line (no value, no trace)."""


@dataclass(frozen=True)
class ImportSummary:
    pack_id: str
    slug: str
    created: bool
    version: int
    files: int
    skills: int
    ignored: int
    allowed: int = 0


async def workspace_by_ref(session: AsyncSession, ref: str) -> Workspace:
    """A workspace by id or slug."""
    ref = (ref or "").strip()
    row = None
    if ref:
        row = (await session.scalars(select(Workspace).where(
            (Workspace.id == ref) | (Workspace.slug == ref)).limit(1))).first()
    if row is None:
        raise ImportRefusedError("no such workspace")
    return row


async def team_grant(session: AsyncSession, ws_id: str, spec: str) -> tuple[str, str]:
    """``TEAM_ID_OR_NAME[:read|maintain]`` -> (team id, permission)."""
    name, _, permission = (spec or "").rpartition(":")
    if not name or permission not in PACK_PERMISSIONS:
        name, permission = spec, "read"
    name = name.strip()
    teams = (await session.scalars(select(Team).where(
        Team.workspace_id == ws_id,
        (Team.id == name) | (func.lower(Team.name) == name.lower())))).all()
    exact = [t for t in teams if t.id == name] or list(teams)
    if len(exact) != 1:
        raise ImportRefusedError("no such team in that workspace" if not exact
                           else "that team name is not unique; use its id")
    return exact[0].id, permission


async def import_pack(
    session: AsyncSession, principal: PackPrincipal, ws_id: str, *, slug: str, result: Any,
    title: str = "", description: str = "", audience: str = "teams",
    teams: Sequence[tuple[str, str]] = (), note: str = "",
) -> ImportSummary:
    """Create the pack if there is none of that slug, then add ``result`` (an
    ``IngestResult``) as its next version. Raises :class:`store.PackError`, or
    :class:`PackNotAccessible` for a pack the person may not see.

    The order of the checks is the point: a person who may neither create packs
    nor see a pack is told ONE thing - ``pack not found or not accessible`` -
    whether the slug is taken by a pack hidden from them or is free. Asking the
    store to create first would answer "Requires the editor role" only for a free
    slug and so tell them which slugs exist. Somebody who can see the pack, and
    somebody who may create one, learn nothing new."""
    slug = (slug or "").strip().lower()
    existing = await session.scalar(select(KnowledgePack.id).where(
        KnowledgePack.workspace_id == ws_id, KnowledgePack.slug == slug))
    created = existing is None
    if created:
        if not can_create(principal):
            raise PackNotAccessible()
        view = await store.create_pack(
            session, principal, ws_id, slug=slug, title=title or slug,
            description=description, audience=audience, teams=teams)
    else:
        view = await store.get_pack(session, principal, ws_id, existing)
    version = await store.add_version(session, view, result, note)
    return ImportSummary(
        pack_id=view.pack.id, slug=view.pack.slug, created=created, version=version.number,
        files=version.file_count, skills=len(result.skills), ignored=result.ignored_total,
        allowed=len(getattr(result, "allowed", None) or ()))


async def list_for_operator(session: AsyncSession, ws_id: str) -> list[store.PackInfo]:
    """Every pack of the workspace, as the superadmin sees them."""
    views = await store.list_views(session, OPERATOR, ws_id)
    return await store.describe(session, views)


__all__ = ["CLI_ACTOR", "ImportRefusedError", "ImportSummary", "OPERATOR", "import_pack",
           "list_for_operator", "team_grant", "workspace_by_ref"]
