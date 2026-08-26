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

from src.db.models import Workspace, WorkspaceMember

logger = logging.getLogger(__name__)


def personal_slug(user_id: str) -> str:
    """Deterministic, per-user-unique slug used as the idempotency key."""
    return f"u-{user_id}"


def ensure_personal_workspace(
    session: Session,
    user_id: str,
    email: str | None = None,
    name: str | None = None,
) -> str:
    """Return the id of the user's personal workspace, creating it if absent.

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
                return ensure_personal_workspace(s, user_id, email, name)
        finally:
            eng.dispose()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "personal_workspace_provision_failed user=%s err=%s", user_id, exc,
        )
        return None


__all__ = [
    "personal_slug",
    "ensure_personal_workspace",
    "provision_personal_workspace",
]
