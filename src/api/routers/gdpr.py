"""GDPR data export + erasure (Stage 21).

    GET    /api/gdpr/export/{user_id}   — full JSON export (admin, or self)
    DELETE /api/gdpr/user/{user_id}     — erasure: deactivate + anonymise

Export collects everything keyed to the user across stores:
    * users row (SQLite user store)
    * review_runs (SQLite)
    * chats + messages they own (Postgres)
    * oauth refresh tokens (metadata only, hashes excluded)
    * credentials rows (provider names + masked, never secrets)
    * workspace + team memberships

Erasure is SOFT: `is_active=false`, email → `deleted-{id}@erased.local`,
name cleared, credentials rows hard-deleted (they're the user's own
secrets), refresh tokens revoked. Review history and audit records stay
— they are the workspace's legitimate business records; the identity
link is severed via the anonymised email. A JSON audit line records the
erasure itself.

Why this stays GLOBAL admin
---------------------------
A workspace owner administering "their own workspace's data" would be a
reasonable rule if either endpoint were scoped to a workspace. Neither is:
both are keyed on a USER, and a user is not owned by one tenant.

  * The export walks chats by `owner_user_id`, review runs by `user_id`,
    credentials, refresh tokens and the person's memberships — with no
    workspace filter anywhere. Handing it to the owner of one of that
    person's workspaces exports the other tenants' conversations too.
  * The erasure is installation-wide: it deactivates the account, wipes
    every stored credential and revokes every refresh token, in every
    workspace the person belongs to. One tenant's owner must not be able
    to switch off a user another tenant depends on.

Scoping either of these to a workspace is a real piece of work (filter the
export by workspace membership, define what erasure means when the account
survives elsewhere), not a permission-decorator swap. Until then it is a
global-admin function and the page says so.

Non-admins keep the one thing that is unambiguously theirs: `GET
/api/gdpr/export/{own_id}` — a person may always export themselves.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, require_admin
from src.db.models import (
    Chat,
    Message,
    OAuthRefreshToken,
    TeamMember,
    WorkspaceMember,
)
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gdpr", tags=["gdpr"])


@router.get("/export/{user_id}")
async def export_user_data(
    user_id: str,
    session: AsyncSession = Depends(get_async_session),
    caller: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Full personal-data export. Admins may export anyone; users may
    export only themselves."""
    if not caller.is_admin and caller.id != user_id:
        raise HTTPException(status_code=403, detail="may only export own data")

    from src.users import get_user_store
    target = get_user_store().get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")

    # ── user profile
    profile = {
        "id": target.id, "email": target.email, "name": target.name,
        "auth_method": target.auth_method.value,
        "is_admin": target.is_admin, "is_active": target.is_active,
        "scopes": list(target.scopes or []),
        "created_at": str(target.created_at),
        "last_login_at": str(target.last_login_at),
    }

    # ── review runs (SQLite)
    runs: list[dict[str, Any]] = []
    try:
        from src.api.review_runs import get_review_run_store
        store = get_review_run_store()
        with sqlite3.connect(store.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute(
                "SELECT id, pr_ref, status, verdict, findings_count, "
                "       started_at, finished_at, cost_usd "
                "FROM review_runs WHERE user_id = ? ORDER BY started_at DESC",
                (user_id,),
            ):
                runs.append(dict(r))
    except Exception as exc:  # noqa: BLE001
        logger.warning("gdpr_runs_failed err=%s", exc)

    # ── chats + messages
    chats_out: list[dict[str, Any]] = []
    chat_rows = (await session.scalars(
        select(Chat).where(Chat.owner_user_id == user_id)
    )).all()
    for c in chat_rows:
        msgs = (await session.scalars(
            select(Message).where(Message.chat_id == c.id).order_by(Message.id)
        )).all()
        chats_out.append({
            "id": c.id, "name": c.name, "repo_slug": c.repo_slug,
            "project_id": c.project_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "messages": [
                {"role": m.role, "content": m.content,
                 "timestamp": m.timestamp.isoformat() if m.timestamp else None}
                for m in msgs
            ],
        })

    # ── oauth refresh tokens (metadata, no hashes)
    tokens = (await session.scalars(
        select(OAuthRefreshToken).where(OAuthRefreshToken.user_id == user_id)
    )).all()
    tokens_out = [
        {"client_id": t.client_id, "scope": t.scope,
         "issued_at": t.issued_at.isoformat() if t.issued_at else None,
         "expires_at": t.expires_at.isoformat() if t.expires_at else None,
         "revoked": t.revoked}
        for t in tokens
    ]

    # ── credentials (provider names + masked)
    creds_out: list[dict[str, Any]] = []
    try:
        from src.credentials import get_credential_store
        for entry in get_credential_store().list(user_id=user_id):
            creds_out.append({
                "provider": entry.get("provider"),
                "account_label": entry.get("account_label"),
                "updated_at": entry.get("updated_at"),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("gdpr_creds_failed err=%s", exc)

    # ── memberships
    ws_members = (await session.scalars(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
    )).all()
    team_members = (await session.scalars(
        select(TeamMember).where(TeamMember.user_id == user_id)
    )).all()

    logger.info("gdpr_export user=%s by=%s", user_id, caller.email)
    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "profile": profile,
        "review_runs": runs,
        "chats": chats_out,
        "oauth_tokens": tokens_out,
        "credentials": creds_out,
        "workspace_memberships": [
            {"workspace_id": m.workspace_id, "role": m.role} for m in ws_members
        ],
        "team_memberships": [
            {"team_id": m.team_id, "role": m.role} for m in team_members
        ],
    }


@router.delete("/user/{user_id}")
async def erase_user(
    user_id: str,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_admin),
) -> dict[str, Any]:
    """Soft erasure. Irreversible identity unlink — see module docstring."""
    from src.users import get_user_store
    store = get_user_store()
    target = store.get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.is_admin and target.id == admin.id:
        raise HTTPException(status_code=400, detail="cannot erase yourself")

    old_email = target.email
    anonymised = replace(
        target,
        email=f"deleted-{user_id[:12]}@erased.local",
        name="",
        is_active=False,
        google_sub=None,
        password_hash=None,
    )
    store.update(anonymised)

    # Hard-delete personal secrets.
    deleted_creds = 0
    try:
        from src.credentials import get_credential_store
        cstore = get_credential_store()
        for entry in cstore.list(user_id=user_id):
            if cstore.delete(
                provider=entry.get("provider", ""),
                user_id=user_id,
                account_label=entry.get("account_label", "default"),
            ):
                deleted_creds += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("gdpr_creds_delete_failed err=%s", exc)

    # Revoke refresh tokens.
    tokens = (await session.scalars(
        select(OAuthRefreshToken).where(OAuthRefreshToken.user_id == user_id)
    )).all()
    for t in tokens:
        t.revoked = True
    await session.commit()

    logger.info(
        "gdpr_erasure user=%s old_email_hash=%s creds_deleted=%d tokens_revoked=%d by=%s",
        user_id, hash(old_email) & 0xFFFFFFFF, deleted_creds, len(tokens), admin.email,
    )
    return {
        "user_id": user_id,
        "erased": True,
        "credentials_deleted": deleted_creds,
        "tokens_revoked": len(tokens),
    }


__all__ = ["router"]
