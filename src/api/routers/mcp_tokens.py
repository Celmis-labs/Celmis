"""Superadmin-issued MCP tokens, the MCP call log, and a person's own view.

A developer's MCP token is issued by the superadmin for ONE named person with
an explicit list of repositories (exact slugs and/or globs), an expiry and —
only when asked — write access. The list is authoritative: it grants ``code``
on the matching repositories of the token's workspace whether or not a team
rule exists (the path-level deny globs of a rule still conceal secrets). It is
looked up on the server on every call, so a PATCH changes an issued token and a
revocation stops it within the cache window, with no reissue.

  POST   /api/admin/mcp-tokens                issue (the token is shown once)
  POST   /api/admin/mcp-grants                allow a person to connect by OAuth
  GET    /api/admin/mcp-repos?workspace=      repos a pattern can match
  GET    /api/admin/mcp-tokens                list (metadata only; kind says pat / oauth_grant)
  PATCH  /api/admin/mcp-tokens/{id}           repos / expiry / write / label
  POST   /api/admin/mcp-tokens/{id}/revoke    revoke
  GET    /api/admin/mcp-calls                 the call log (+ ``?format=csv``)
  GET    /api/mcp/tokens/me                   my tokens, metadata only
  POST   /api/mcp/tokens/{id}/revoke          a holder revokes their own

Issuing, changing and reading the log need the superadmin
(``CELMIS_MCP_TOKEN_ISSUERS=platform_admin`` widens it to any global admin).
Nothing here stores or logs a token value.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.api.deps import client_ip, get_current_user, get_users
from src.users import User, UserStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["mcp-tokens"])
me_router = APIRouter(prefix="/api/mcp", tags=["mcp"])

_NOT_ISSUER = "Only the superadmin issues MCP tokens."


# ─── models ──────────────────────────────────────────────────────────


class McpTokenIssueIn(BaseModel):
    #: Who the token is for: the account's email or id.
    user_ref: str = Field(min_length=1, max_length=200)
    #: The workspace the token answers for: its id or slug.
    workspace_id: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=80)
    #: Exact slugs and/or globs ("acme/shop-*"). "*" = every repo of the workspace.
    repos: list[str] = Field(min_length=1, max_length=200)
    #: Needs profile "full": the dev endpoint is read-only.
    allow_write: bool = False
    expires_in_days: int = Field(default=30, ge=1, le=3650)
    profile: str = Field(default="dev", pattern="^(dev|full)$")


class McpTokenPatchIn(BaseModel):
    repos: list[str] | None = Field(default=None, max_length=200)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    allow_write: bool | None = None
    label: str | None = Field(default=None, max_length=80)


class McpTokenOut(BaseModel):
    id: str
    kind: str
    workspace_id: str
    user_id: str
    user_email: str = ""
    issued_by: str = ""
    label: str = ""
    repos: list[str]
    matched_repos: list[str] = []
    #: Sentences about entries that reach further than they look (a full name
    #: shared by the same repository on two providers).
    warnings: list[str] = []
    allow_write: bool
    profile: str
    created_at: str | None = None
    expires_at: str
    revoked_at: str | None = None
    last_used_at: str | None = None
    status: str


class McpTokenIssuedOut(McpTokenOut):
    #: The token value. Returned ONCE, here; it is not stored and not logged.
    token: str
    url: str
    #: A `.mcp.json` ready to paste. The token is NOT in it: it reads the
    #: CELMIS_MCP_TOKEN environment variable.
    mcp_json: dict


class McpMyTokensOut(BaseModel):
    self_service_enabled: bool
    max_days: int
    tokens: list[McpTokenOut]


class McpCallOut(BaseModel):
    ts: str
    workspace_id: str
    user_id: str
    token_id: str | None = None
    kind: str
    client_id: str = ""
    tool: str
    profile: str
    repos: list[str]
    status: str
    result_bytes: int
    result_items: int
    duration_ms: int


# ─── helpers ─────────────────────────────────────────────────────────


def _require_issuer(user: User = Depends(get_current_user)) -> User:
    from src.mcp_server import token_store

    if not token_store.may_issue(user):
        raise HTTPException(status_code=403, detail=_NOT_ISSUER)
    return user


def _session():  # noqa: ANN202
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine

    return Session(_sync_engine())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _status(view) -> str:  # noqa: ANN001
    if view.revoked_at is not None:
        return "revoked"
    if view.expires_at <= datetime.now(UTC):
        return "expired"
    return "active"


def _out(view, users: UserStore, *, with_matches: bool = False) -> dict:  # noqa: ANN001
    holder = users.get_by_id(view.user_id)
    data = {
        "id": view.id, "kind": view.kind, "workspace_id": view.workspace_id,
        "user_id": view.user_id, "user_email": holder.email if holder else "",
        "issued_by": view.issued_by, "label": view.label,
        "repos": list(view.patterns), "allow_write": view.allow_write,
        "profile": view.profile, "created_at": _iso(view.created_at),
        "expires_at": view.expires_at.isoformat(),
        "revoked_at": _iso(view.revoked_at),
        "last_used_at": _iso(view.last_used_at), "status": _status(view),
        "matched_repos": [],
    }
    if with_matches:
        from src.access.effective import matched_repos

        data["matched_repos"] = matched_repos(view.patterns, view.workspace_id)
        data["warnings"] = _ambiguity_warnings(view.patterns, view.workspace_id)
    return data


def _ambiguity_warnings(patterns, workspace_id: str) -> list[str]:  # noqa: ANN001
    """An entry that is not an exact slug but matches several slugs through a
    shared ``owner/name`` (the same repository on GitHub and on GitLab) grants
    all of them. Say so, and say what to list instead."""
    from src.access.effective import pattern_matches, registered_repos

    registry = registered_repos(workspace_id)
    out: list[str] = []
    for pat in patterns:
        if pat in registry or any(c in pat for c in "*?["):
            continue
        hits = sorted(slug for slug, names in registry.items()
                      if pattern_matches(pat, *names))
        if len(hits) > 1:
            out.append(f"{pat} matches {len(hits)} repositories ({', '.join(hits)}); "
                       "list exact slugs to grant only one of them.")
    return out


def _resolve_workspace(session, ref: str):  # noqa: ANN001, ANN202
    from sqlalchemy import or_, select

    from src.db.models import Workspace

    return session.execute(
        select(Workspace).where(or_(Workspace.id == ref, Workspace.slug == ref))
    ).scalars().first()


def _resolve_user(users: UserStore, ref: str) -> User | None:
    ref = ref.strip()
    user = users.get_by_id(ref)
    if user is None and "@" in ref and hasattr(users, "get_by_email"):
        user = users.get_by_email(ref)
    return user if user is not None and getattr(user, "is_active", True) else None


def _audit(action: str, actor: User, workspace_id: str, token_id: str, detail: dict,
           request=None) -> None:  # noqa: ANN001
    try:
        from src.security.audit import record_action

        record_action(action=action, actor=actor.email, actor_id=actor.id,
                      workspace_id=workspace_id, target=token_id,
                      ip=client_ip(request) if request is not None else None,
                      detail=detail)
    except Exception:  # noqa: BLE001 — never block the operation it records
        logger.warning("mcp_token_audit_failed action=%s", action)


def _public_base() -> str:
    try:
        from src.config import get_settings

        return str(getattr(get_settings(), "public_base_url", "") or "").rstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def _endpoint(profile: str) -> str:
    base = _public_base()
    # Trailing slash on purpose: without it Starlette answers 307, and a
    # redirected POST is not something every MCP client follows.
    path = "/mcp/dev/" if profile == "dev" else "/mcp/"
    return f"{base}{path}" if base else path


def _mcp_json(profile: str) -> dict:
    return {"mcpServers": {"celmis": {
        "type": "http", "url": _endpoint(profile),
        "headers": {"Authorization": "Bearer ${CELMIS_MCP_TOKEN}"},
    }}}


# ─── issue / list / change / revoke ──────────────────────────────────


@router.post("/mcp-tokens", response_model=McpTokenIssuedOut, status_code=201)
def issue_token(
    payload: McpTokenIssueIn,
    user: User = Depends(_require_issuer),
    users: UserStore = Depends(get_users),
) -> McpTokenIssuedOut:
    from src.mcp_server import token_store
    from src.mcp_server.auth import JwtConfigError

    holder = _resolve_user(users, payload.user_ref)
    if holder is None:
        raise HTTPException(status_code=404, detail="No such active user.")
    with _session() as s:
        ws = _resolve_workspace(s, payload.workspace_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="No such workspace.")
        from src.db.models import WorkspaceMember

        member = s.get(WorkspaceMember, (ws.id, holder.id))
        if member is None and not holder.is_admin:
            raise HTTPException(
                status_code=422,
                detail="That person is not a member of this workspace; add them first.")
        workspace_id = ws.id
    try:
        token, view = token_store.mint(
            kind="pat", workspace_id=workspace_id, user_id=holder.id,
            issued_by=user.id, label=payload.label or f"{holder.email} dev",
            patterns=payload.repos, allow_write=payload.allow_write,
            profile=payload.profile, expires_in_days=payload.expires_in_days,
        )
    except token_store.TokenError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except JwtConfigError as exc:
        raise HTTPException(
            status_code=503,
            detail=("MCP is not configured on this server: set MCP_JWT_SECRET "
                    "(or CELMIS_JWT_SECRET) and restart. " + str(exc)[:200])) from exc
    _audit("mcp.token_issued", user, workspace_id, view.id, {
        "holder": holder.id, "repos": list(view.patterns),
        "allow_write": view.allow_write, "profile": view.profile,
        "expires_at": view.expires_at.isoformat()})
    logger.info("mcp_token_issued id=%s holder=%s ws=%s by=%s", view.id, holder.id,
                workspace_id, user.id)
    return McpTokenIssuedOut(
        **_out(view, users, with_matches=True), token=token,
        url=_endpoint(view.profile), mcp_json=_mcp_json(view.profile))


class McpGrantIn(BaseModel):
    user_ref: str = Field(min_length=1, max_length=200)
    workspace_id: str = Field(min_length=1, max_length=200)
    repos: list[str] = Field(min_length=1, max_length=200)
    allow_write: bool = False
    expires_in_days: int = Field(default=30, ge=1, le=3650)


@router.post("/mcp-grants", response_model=McpTokenOut, status_code=201)
def issue_grant(
    payload: McpGrantIn,
    user: User = Depends(_require_issuer),
    users: UserStore = Depends(get_users),
) -> McpTokenOut:
    """Let one person connect through the browser (OAuth) to the listed repos.

    OAuth keeps working, but only for a person who has such a row: the consent
    step refuses without it and every OAuth token is limited to its repos. No
    token value exists here — the person signs in and the client gets its own.
    """
    from src.db.models import WorkspaceMember
    from src.mcp_server import token_store

    holder = _resolve_user(users, payload.user_ref)
    if holder is None:
        raise HTTPException(status_code=404, detail="No such active user.")
    with _session() as s:
        ws = _resolve_workspace(s, payload.workspace_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="No such workspace.")
        if s.get(WorkspaceMember, (ws.id, holder.id)) is None and not holder.is_admin:
            raise HTTPException(
                status_code=422,
                detail="That person is not a member of this workspace; add them first.")
        try:
            row = token_store.create_row(
                s, kind="oauth_grant", workspace_id=ws.id, user_id=holder.id,
                issued_by=user.id, label="oauth", patterns=payload.repos,
                allow_write=payload.allow_write,
                profile="full" if payload.allow_write else "dev",
                expires_in_days=payload.expires_in_days)
        except token_store.TokenError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        view = token_store._view(row)
    _audit("mcp.grant_issued", user, view.workspace_id, view.id, {
        "holder": holder.id, "repos": list(view.patterns), "allow_write": view.allow_write})
    return McpTokenOut(**_out(view, users, with_matches=True))


class McpRepoOut(BaseModel):
    slug: str
    full_name: str = ""


@router.get("/mcp-repos", response_model=list[McpRepoOut])
def list_workspace_repos(
    workspace: str = Query(min_length=1, max_length=200),
    _user: User = Depends(_require_issuer),
) -> list[McpRepoOut]:
    """The repositories registered in a workspace, for the picker on the token
    page. Only the issuer sees it: it is the list a token's patterns match."""
    from src.access.effective import registered_repos

    with _session() as s:
        ws = _resolve_workspace(s, workspace)
    if ws is None:
        raise HTTPException(status_code=404, detail="No such workspace.")
    return [McpRepoOut(slug=slug, full_name=next((n for n in names if n != slug), ""))
            for slug, names in sorted(registered_repos(ws.id).items())]


@router.get("/mcp-tokens", response_model=list[McpTokenOut])
def list_tokens(
    user_ref: str | None = Query(default=None, alias="user"),
    workspace: str | None = None,
    status: str | None = Query(default=None, pattern="^(active|revoked|expired)$"),
    _user: User = Depends(_require_issuer),
    users: UserStore = Depends(get_users),
) -> list[McpTokenOut]:
    from src.mcp_server import token_store

    holder_id = None
    if user_ref:
        holder = _resolve_user(users, user_ref)
        holder_id = holder.id if holder else user_ref
    with _session() as s:
        ws_id = None
        if workspace:
            ws = _resolve_workspace(s, workspace)
            ws_id = ws.id if ws else workspace
        views = token_store.list_rows(
            s, user_id=holder_id, workspace_id=ws_id, status=status,
            kinds=token_store.KINDS)
    return [McpTokenOut(**_out(v, users, with_matches=True)) for v in views[:500]]


@router.patch("/mcp-tokens/{token_id}", response_model=McpTokenOut)
def patch_token(
    token_id: str,
    payload: McpTokenPatchIn,
    user: User = Depends(_require_issuer),
    users: UserStore = Depends(get_users),
) -> McpTokenOut:
    from src.mcp_server import token_store

    with _session() as s:
        try:
            row = token_store.update_row(
                s, token_id, patterns=payload.repos,
                expires_in_days=payload.expires_in_days,
                allow_write=payload.allow_write, label=payload.label)
        except token_store.TokenError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="No such token.")
        view = token_store._view(row)
    _audit("mcp.token_updated", user, view.workspace_id, token_id, {
        "repos": list(view.patterns), "allow_write": view.allow_write,
        "expires_at": view.expires_at.isoformat()})
    return McpTokenOut(**_out(view, users, with_matches=True))


@router.post("/mcp-tokens/{token_id}/revoke", response_model=McpTokenOut)
def revoke_token(
    token_id: str,
    user: User = Depends(_require_issuer),
    users: UserStore = Depends(get_users),
) -> McpTokenOut:
    from src.mcp_server import token_store

    with _session() as s:
        row = token_store.revoke(s, token_id, by=user.id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such token.")
        view = token_store._view(row)
    _audit("mcp.token_revoked", user, view.workspace_id, token_id, {"holder": view.user_id})
    return McpTokenOut(**_out(view, users))


# ─── the call log ────────────────────────────────────────────────────

_CSV_COLUMNS = ("ts", "workspace_id", "user_id", "token_id", "kind", "tool", "profile",
                "status", "repos", "result_bytes", "result_items", "duration_ms")


def _csv_cell(value) -> str:  # noqa: ANN001
    text = str(value if value is not None else "")
    # A spreadsheet runs a cell that starts with one of these as a formula.
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/mcp-calls", response_model=list[McpCallOut])
def list_calls(
    user_id: str | None = Query(default=None, alias="user"),
    token_id: str | None = Query(default=None, alias="token"),
    tool: str | None = None,
    status: str | None = Query(default=None, pattern="^(ok|denied|error)$"),
    workspace: str | None = None,
    days: int = Query(default=7, ge=1, le=3650),
    limit: int = Query(default=200, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    format: str = Query(default="json", pattern="^(json|csv)$"),  # noqa: A002
    _user: User = Depends(_require_issuer),
):
    from sqlalchemy import select

    from src.db.models import McpCallLog

    stmt = select(McpCallLog).where(
        McpCallLog.ts >= datetime.now(UTC) - timedelta(days=days))
    if user_id:
        stmt = stmt.where(McpCallLog.user_id == user_id)
    if token_id:
        stmt = stmt.where(McpCallLog.token_id == token_id)
    if tool:
        stmt = stmt.where(McpCallLog.tool == tool)
    if status:
        stmt = stmt.where(McpCallLog.status == status)
    stmt = stmt.order_by(McpCallLog.ts.desc()).limit(limit).offset(offset)
    with _session() as s:
        if workspace:
            ws = _resolve_workspace(s, workspace)
            stmt = stmt.where(McpCallLog.workspace_id == (ws.id if ws else workspace))
        rows = s.execute(stmt).scalars().all()
        calls = [{
            "ts": r.ts.isoformat(), "workspace_id": r.workspace_id, "user_id": r.user_id,
            "token_id": r.token_id, "kind": r.kind, "client_id": r.client_id,
            "tool": r.tool, "profile": r.profile, "repos": list(r.repos or []),
            "status": r.status, "result_bytes": r.result_bytes,
            "result_items": r.result_items, "duration_ms": r.duration_ms,
        } for r in rows]
    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_COLUMNS)
        for c in calls:
            writer.writerow([_csv_cell(";".join(c[k]) if k == "repos" else c[k])
                             for k in _CSV_COLUMNS])
        return Response(
            content=buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="mcp-calls.csv"'})
    return calls


# ─── a person's own tokens ───────────────────────────────────────────


@me_router.get("/tokens/me", response_model=McpMyTokensOut)
def my_tokens(
    user: User = Depends(get_current_user),
    users: UserStore = Depends(get_users),
) -> McpMyTokensOut:
    """Metadata of the caller's own tokens — never a value, never somebody else's."""
    from src.mcp_server import token_store

    with _session() as s:
        views = token_store.list_rows(s, user_id=user.id, kinds=("pat", "cli", "self"))
    return McpMyTokensOut(
        self_service_enabled=token_store.self_service_enabled(),
        max_days=token_store.max_days(),
        tokens=[McpTokenOut(**_out(v, users)) for v in views[:100]])


@me_router.post("/tokens/{token_id}/revoke", response_model=McpTokenOut)
def revoke_my_token(
    token_id: str,
    user: User = Depends(get_current_user),
    users: UserStore = Depends(get_users),
) -> McpTokenOut:
    """A holder may revoke their own token. A token that is not theirs is
    answered like one that does not exist."""
    from src.mcp_server import token_store

    with _session() as s:
        view = token_store.lookup_fresh(s, token_id)
        if view is None or view.user_id != user.id or view.kind == "oauth_grant":
            raise HTTPException(status_code=404, detail="No such token.")
        row = token_store.revoke(s, token_id, by=user.id)
        view = token_store._view(row)
    _audit("mcp.token_revoked", user, view.workspace_id, token_id, {"holder": user.id, "self": True})
    return McpTokenOut(**_out(view, users))


__all__ = ["me_router", "router"]
