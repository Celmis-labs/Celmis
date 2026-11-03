"""Lazy per-user workspace provisioning (multi-tenant).

Every authenticated user owns a personal workspace. It is created idempotently
by slug ``u-{user_id}`` (the full user id, so two users can never collide onto
the same slug and get bound into one tenant). The credential/config slots key
off the workspace **id**, so a user's LLM/git keys land in their own tenant.

Provisioned proactively at signup/login/OAuth and, as a safety net, on first
workspace resolution (``deps.current_workspace_id``) for any pre-existing user.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import DeletedPersonalWorkspace, Workspace, WorkspaceMember

logger = logging.getLogger(__name__)


def personal_slug(user_id: str) -> str:
    """Deterministic, per-user-unique slug used as the idempotency key."""
    return f"u-{user_id}"


def personal_owner_id(slug: str) -> str | None:
    """The user id a personal workspace slug (``u-{user_id}``) names, else None
    (a team workspace)."""
    return slug[2:] if slug.startswith("u-") and len(slug) > 2 else None


def is_dead_personal(slug: str, is_active_by_id: dict[str, bool]) -> bool:
    """A personal workspace whose person is gone (no account) or switched off.

    Workspaces have no "deleted" flag: deleting one removes the row. What stays
    behind is the personal workspace of an account that was erased from the user
    store (a separate database) or deactivated, which nobody can sign in to. Such
    a workspace is not offered for new tokens and a token is not issued for it.
    ``is_active_by_id`` maps every known account id to its ``is_active``."""
    owner = personal_owner_id(slug)
    return owner is not None and not is_active_by_id.get(owner, False)


def ensure_personal_workspace(
    session: Session,
    user_id: str,
    email: str | None = None,
    name: str | None = None,
    *,
    honour_deletion: bool = False,
) -> str | None:
    """Return the id of the user's personal workspace, creating it if absent.

    ``honour_deletion``: an admin deleted this workspace on purpose (a row in
    ``deleted_personal_workspaces``). Sign-in passes True and gets ``None`` back,
    leaving it deleted, as long as the person belongs to some other workspace. The
    no-membership fallback (``deps.current_workspace_id``) passes False: that
    person must not land in the shared tenant, so it is created and the marker cleared.

    Idempotent by slug. Uses the provided **sync** session and commits on
    create. On a concurrent create the unique-slug constraint fires; we roll
    back and re-read by slug — which, because the slug embeds the FULL user id,
    always returns *this* user's workspace, never another tenant's.
    """
    slug = personal_slug(user_id)
    existing = session.execute(
        select(Workspace).where(Workspace.slug == slug)
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id

    tomb = session.get(DeletedPersonalWorkspace, user_id)
    if tomb is not None and honour_deletion and session.execute(
        select(WorkspaceMember.workspace_id)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user_id).limit(1)
    ).first() is not None:
        return None

    display = (name or (email.split("@", 1)[0] if email else "") or "user").strip() or "user"
    ws_id = str(uuid.uuid4())
    ws_name = f"{display}'s workspace"
    # `name` is unique too — disambiguate a display collision between users.
    if session.execute(
        select(Workspace).where(Workspace.name == ws_name)
    ).scalar_one_or_none() is not None:
        ws_name = f"{display}'s workspace ({user_id[:8]})"

    session.add(Workspace(
        id=ws_id, name=ws_name, slug=slug, description="", created_by=email,
    ))
    session.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role="owner"))
    if tomb is not None:
        session.delete(tomb)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.execute(
            select(Workspace).where(Workspace.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        raise
    logger.info(
        "personal_workspace_provisioned user=%s workspace=%s slug=%s",
        user_id, ws_id, slug,
    )
    return ws_id


def provision_personal_workspace(
    user_id: str, email: str | None = None, name: str | None = None,
) -> str | None:
    """Self-contained variant for callers without a DB session (signup/login/
    OAuth). Opens a short-lived sync engine. Never raises — returns None on
    failure so it can't block authentication."""
    from src.db.session import get_database_url

    try:
        sync_url = get_database_url().replace(
            "postgresql+asyncpg://", "postgresql+psycopg://"
        )
        eng = create_engine(sync_url, pool_pre_ping=True)
        try:
            with Session(eng) as s:
                return ensure_personal_workspace(
                    s, user_id, email, name, honour_deletion=True)
        finally:
            eng.dispose()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "personal_workspace_provision_failed user=%s err=%s", user_id, exc,
        )
        return None


__all__ = [
    "personal_slug",
    "personal_owner_id",
    "is_dead_personal",
    "ensure_personal_workspace",
    "provision_personal_workspace",
]
