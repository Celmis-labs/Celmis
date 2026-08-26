"""Cross-repo intelligence endpoints (Stage 15).

    GET    /api/intel/ownership/{repo_slug:path}      — read snapshot
    POST   /api/intel/ownership/{repo_slug:path}/rebuild
    GET    /api/intel/architecture/{repo_slug:path}    — read summary
    POST   /api/intel/architecture/{repo_slug:path}/rebuild

    GET    /api/intel/deprecations                     — list
    POST   /api/intel/deprecations                     — create
    PUT    /api/intel/deprecations/{id}                — update
    DELETE /api/intel/deprecations/{id}
    POST   /api/intel/deprecations/{id}/scan           — refresh consumers

    GET    /api/notifications/channels
    POST   /api/notifications/channels
    DELETE /api/notifications/channels/{id}
    POST   /api/notifications/channels/{id}/test
    GET    /api/notifications/bindings
    POST   /api/notifications/bindings
    DELETE /api/notifications/bindings/{id}
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    current_workspace_id,
    get_current_user,
    require_admin,
    require_repo_permission,
    require_workspace_admin,
)
from src.db.models import (
    ChannelBinding,
    DeprecatedSymbol,
    NotificationChannel,
    OwnershipSnapshot,
    RepoSummary,
)
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

intel_router = APIRouter(prefix="/api/intel", tags=["intel"])
notif_router = APIRouter(prefix="/api/notifications", tags=["notifications"])


# ═══ Ownership ═══════════════════════════════════════════════════════


class OwnershipOut(BaseModel):
    repo_slug: str
    computed_at: str | None
    lookback_days: int
    stats: dict
    paths: dict  # capped in the response


@intel_router.get("/ownership/{repo_slug:path}", response_model=OwnershipOut)
async def get_ownership(
    repo_slug: str,
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    _perm: User = Depends(require_repo_permission("read")),
) -> OwnershipOut:
    row = (await session.scalars(
        select(OwnershipSnapshot)
        .where(OwnershipSnapshot.repo_slug == repo_slug)
        .order_by(OwnershipSnapshot.computed_at.desc())
        .limit(1)
    )).first()
    if row is None:
        return OwnershipOut(
            repo_slug=repo_slug, computed_at=None,
            lookback_days=0, stats={}, paths={},
        )
    paths = dict(row.paths or {})
    # Cap payload — snapshots can be tens of thousands of entries.
    if len(paths) > 500:
        paths = dict(list(paths.items())[:500])
    return OwnershipOut(
        repo_slug=row.repo_slug,
        computed_at=row.computed_at.isoformat() if row.computed_at else None,
        lookback_days=row.lookback_days,
        stats=dict(row.stats or {}),
        paths=paths,
    )


@intel_router.post("/ownership/{repo_slug:path}/rebuild")
async def rebuild_ownership(
    repo_slug: str,
    lookback_days: int = Query(default=90, ge=1, le=730),
    user: User = Depends(get_current_user),
    _perm: User = Depends(require_repo_permission("review")),
) -> dict[str, Any]:
    import asyncio

    from src.ownership import compute_ownership
    # git-blame over up to 400 files is slow and synchronous — keep the
    # event loop alive.
    snap_id = await asyncio.to_thread(
        compute_ownership,
        repo_slug, lookback_days=lookback_days, computed_by=user.email,
    )
    if snap_id is None:
        raise HTTPException(status_code=400,
                            detail=f"repo {repo_slug!r} not cloned locally")
    return {"snapshot_id": snap_id, "repo_slug": repo_slug}


# ═══ Architecture summary ════════════════════════════════════════════


class ArchOut(BaseModel):
    repo_slug: str
    summary_md: str
    model_used: str | None
    computed_at: str | None


@intel_router.get("/architecture/{repo_slug:path}", response_model=ArchOut)
async def get_architecture(
    repo_slug: str,
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    _perm: User = Depends(require_repo_permission("read")),
) -> ArchOut:
    row = await session.get(RepoSummary, repo_slug)
    if row is None:
        return ArchOut(repo_slug=repo_slug, summary_md="",
                       model_used=None, computed_at=None)
    return ArchOut(
        repo_slug=row.repo_slug, summary_md=row.summary_md,
        model_used=row.model_used,
        computed_at=row.computed_at.isoformat() if row.computed_at else None,
    )


@intel_router.get("/reverse-index/{repo_slug:path}")
async def get_reverse_index(
    repo_slug: str,
    _user: User = Depends(get_current_user),
    _perm: User = Depends(require_repo_permission("read")),
) -> dict:
    """Return {source_file → [note_paths]} for `repo_slug`. Rebuilt on demand."""
    from src.vault.reverse_index import build_reverse_index
    idx = build_reverse_index(repo_slug, force=True)
    return {
        "repo_slug": repo_slug,
        "source_file_count": len(idx),
        "note_count": len({n for notes in idx.values() for n in notes}),
        "index": idx,
    }


@intel_router.post("/architecture/{repo_slug:path}/rebuild", response_model=ArchOut)
async def rebuild_architecture(
    repo_slug: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
    _perm: User = Depends(require_repo_permission("review")),
) -> ArchOut:
    """Generate a fresh architecture summary. One LLM call using workspace
    active model — safe to call from UI (single-shot, not agent pipeline)."""
    from src.review.architecture import generate_summary

    summary_md, model_used, tokens = generate_summary(
        repo_slug=repo_slug, user_id=user.id, workspace_id=workspace_id,
    )
    # Empty means the LLM call failed or the repo has no readable clone.
    # Storing it would overwrite a good summary with nothing and report
    # success; the previous behaviour was a green toast above an empty card.
    if not (summary_md or "").strip():
        logger.warning("arch_rebuild_empty repo=%s ws=%s", repo_slug, workspace_id)
        raise HTTPException(
            status_code=502,
            detail=("Could not build the architecture summary. The model call "
                    "failed, or this repository has no indexed clone to read. "
                    "Check LLM Setup, then index the repository and try again."),
        )
    row = await session.get(RepoSummary, repo_slug)
    if row is None:
        row = RepoSummary(repo_slug=repo_slug, summary_md=summary_md,
                          model_used=model_used, token_count=tokens,
                          computed_by=user.email)
        session.add(row)
    else:
        row.summary_md = summary_md
        row.model_used = model_used
        row.token_count = tokens
        row.computed_at = datetime.now(UTC)
        row.computed_by = user.email
    await session.commit()
    await session.refresh(row)
    return ArchOut(
        repo_slug=row.repo_slug, summary_md=row.summary_md,
        model_used=row.model_used,
        computed_at=row.computed_at.isoformat() if row.computed_at else None,
    )


# ═══ Deprecations ════════════════════════════════════════════════════


class DeprecationIn(BaseModel):
    repo_slug: str
    symbol: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=2000)
    replacement: str | None = Field(default=None, max_length=500)
    target_removal_at: str | None = None
    model_config = ConfigDict(extra="forbid")


class DeprecationOut(BaseModel):
    id: str
    repo_slug: str
    symbol: str
    reason: str
    replacement: str | None
    target_removal_at: str | None
    deprecated_at: str
    deprecated_by: str | None
    last_scan_at: str | None
    consumers: list
    model_config = ConfigDict(from_attributes=True)


def _dep_to_out(row: DeprecatedSymbol) -> DeprecationOut:
    return DeprecationOut(
        id=row.id, repo_slug=row.repo_slug, symbol=row.symbol,
        reason=row.reason, replacement=row.replacement,
        target_removal_at=row.target_removal_at.isoformat() if row.target_removal_at else None,
        deprecated_at=row.deprecated_at.isoformat(),
        deprecated_by=row.deprecated_by,
        last_scan_at=row.last_scan_at.isoformat() if row.last_scan_at else None,
        consumers=list(row.consumers or []),
    )


@intel_router.get("/deprecations", response_model=list[DeprecationOut])
async def list_deprecations(
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[DeprecationOut]:
    rows = (await session.scalars(
        select(DeprecatedSymbol)
        .where(DeprecatedSymbol.workspace_id == ws_id)
        .order_by(DeprecatedSymbol.repo_slug, DeprecatedSymbol.symbol)
    )).all()
    return [_dep_to_out(r) for r in rows]


@intel_router.post("/deprecations", response_model=DeprecationOut, status_code=201)
async def create_deprecation(
    payload: DeprecationIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_admin),
    ws_id: str = Depends(current_workspace_id),
) -> DeprecationOut:
    target_dt: datetime | None = None
    if payload.target_removal_at:
        try:
            target_dt = datetime.fromisoformat(payload.target_removal_at)
        except ValueError:
            # The ISO parse error adds nothing the caller can act on.
            raise HTTPException(
                status_code=400, detail="invalid target_removal_at",
            ) from None
    row = DeprecatedSymbol(
        id=str(uuid.uuid4()),
        repo_slug=payload.repo_slug,
        symbol=payload.symbol,
        reason=payload.reason,
        replacement=payload.replacement,
        target_removal_at=target_dt,
        deprecated_by=user.email,
        workspace_id=ws_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info("deprecation_created id=%s symbol=%s by=%s",
                row.id, payload.symbol, user.email)
    return _dep_to_out(row)


@intel_router.delete("/deprecations/{dep_id}", status_code=204)
async def delete_deprecation(
    dep_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_admin),
) -> None:
    row = await session.get(DeprecatedSymbol, dep_id)
    if row is None:
        return
    await session.delete(row)
    await session.commit()
    logger.info("deprecation_deleted id=%s by=%s", dep_id, user.email)


@intel_router.post("/deprecations/{dep_id}/scan", response_model=DeprecationOut)
async def scan_deprecation(
    dep_id: str,
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_admin),
) -> DeprecationOut:
    """Refresh consumers list — searches across all indexed repos for
    usages of `symbol`. Simple grep-style scan via the graph tools."""
    row = await session.get(DeprecatedSymbol, dep_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    consumers = _scan_consumers(row.symbol)
    row.consumers = consumers
    row.last_scan_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    return _dep_to_out(row)


def _scan_consumers(symbol: str) -> list[dict[str, Any]]:
    """Best-effort scan across every indexed repo using the mcp_server
    legacy tools (which read the tree-sitter graph)."""
    try:
        from src.mcp_server import tools as legacy
    except Exception:  # noqa: BLE001
        return []
    consumers: list[dict[str, Any]] = []
    try:
        repos = legacy.list_repos()
    except Exception:  # noqa: BLE001
        return []
    for r in repos:
        try:
            res = legacy.find_callers(symbol_id=symbol, repo_slug=r.slug)
            callers = (res or {}).get("callers", []) if isinstance(res, dict) else []
        except Exception:  # noqa: BLE001
            continue
        for c in callers:
            consumers.append({
                "repo_slug": r.slug,
                "symbol": c.get("name"),
                "file": c.get("file", ""),
                "line": c.get("start_line", 0),
            })
    return consumers


# ═══ Notification channels ═══════════════════════════════════════════


#: Which host a channel kind actually posts to. A webhook URL announces its
#: provider in its hostname, so a kind that disagrees with the host is a
#: mistake the form can catch before the first alert is lost.
_KIND_HOSTS = {
    "slack": ("hooks.slack.com",),
    "discord": ("discord.com", "discordapp.com"),
    "google_chat": ("chat.googleapis.com",),
}


def _kind_matches_url(kind: str, url: str) -> str | None:
    """The kind the URL belongs to, when it disagrees with `kind`.

    A Google Chat URL saved under `kind: slack` is accepted by every layer:
    the pattern allows both values, the URL is a valid string, the row stores
    fine. It fails at the first send with a 400 from a provider that was never
    asked — and until somebody presses Test, the only symptom is alerts that
    quietly never arrive.

    `webhook` is deliberately absent: it means "some endpoint of my own", and
    that has no host to check.
    """
    if kind == "webhook":
        return None
    for other, hosts in _KIND_HOSTS.items():
        if any(h in url for h in hosts):
            return None if other == kind else other
    return None


class ChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(slack|discord|google_chat|webhook)$")
    webhook_url: str = Field(min_length=8, max_length=1000)
    config: dict = Field(default_factory=dict)
    enabled: bool = True
    model_config = ConfigDict(extra="forbid")


class ChannelOut(BaseModel):
    id: str
    name: str
    kind: str
    webhook_url: str    # masked in the UI, stored raw
    config: dict
    enabled: bool
    created_at: str
    created_by: str | None


def _chan_out(row: NotificationChannel) -> ChannelOut:
    return ChannelOut(
        id=row.id, name=row.name, kind=row.kind,
        webhook_url=_mask(row.webhook_url),
        config=dict(row.config or {}),
        enabled=row.enabled,
        created_at=row.created_at.isoformat() if row.created_at else "",
        created_by=row.created_by,
    )


def _mask(url: str) -> str:
    if len(url) < 20:
        return "•" * len(url)
    return url[:12] + "…" + url[-6:]


@notif_router.get("/channels", response_model=list[ChannelOut])
async def list_channels(
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[ChannelOut]:
    rows = (await session.scalars(
        select(NotificationChannel)
        .where(NotificationChannel.workspace_id == ws_id)
        .order_by(NotificationChannel.name)
    )).all()
    return [_chan_out(r) for r in rows]


@notif_router.post("/channels", response_model=ChannelOut, status_code=201)
async def create_channel(
    payload: ChannelIn,
    session: AsyncSession = Depends(get_async_session),
    # A channel is workspace-scoped — it is created with `workspace_id=ws_id`
    # two lines down and only ever listed within that workspace. Requiring a
    # PLATFORM admin to make one meant the person who owns the workspace got
    # "Admin scope required" and could not set up their own alerting at all;
    # the test button below had nothing to test.
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> ChannelOut:
    mismatch = _kind_matches_url(payload.kind, payload.webhook_url)
    if mismatch:
        raise HTTPException(
            status_code=422,
            detail=(f"That looks like a {mismatch.replace('_', ' ')} webhook, "
                    f"but the channel kind is {payload.kind}. Each provider "
                    f"expects its own message format — pick "
                    f"{mismatch.replace('_', ' ')}, or use 'webhook' for a "
                    f"plain endpoint of your own."),
        )
    row = NotificationChannel(
        id=str(uuid.uuid4()),
        name=payload.name, kind=payload.kind,
        webhook_url=payload.webhook_url,
        config=payload.config, enabled=payload.enabled,
        created_by=user.email,
        workspace_id=ws_id,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info("channel_created id=%s kind=%s by=%s", row.id, row.kind, user.email)
    return _chan_out(row)


@notif_router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel(
    channel_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    row = await session.get(NotificationChannel, channel_id)
    # Loaded by id with no tenant check until now: `list_channels` filters by
    # workspace and this did not, so an id was enough to delete another
    # tenant's channel. Silent no-op rather than 404, matching the
    # missing-row branch — a 404 here would confirm the id exists elsewhere.
    if row is None or row.workspace_id != ws_id:
        return
    await session.delete(row)
    await session.commit()
    logger.info("channel_deleted id=%s by=%s", channel_id, user.email)


@notif_router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Send one message to the channel, to prove the wiring end to end.

    Workspace-scoped like everything else here: this POSTs to a webhook URL,
    and an unscoped id would let one tenant put text into another tenant's
    chat room.
    """
    row = await session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="not found")
    from src.notifications.dispatch import _send  # type: ignore
    try:
        _send(
            {"kind": row.kind, "webhook_url": row.webhook_url,
             "config": dict(row.config or {}), "name": row.name},
            title="Celmis test message",
            body_md=(f"Test from **{user.email}**. If you see this, the "
                     f"channel is wired correctly."),
            severity="info", link_url=None, extra=None,
        )
    except Exception as exc:  # noqa: BLE001
        # THE WEBHOOK URL IS THE CREDENTIAL. `str(exc)` on an httpx error
        # includes the request URL, and for Google Chat that URL carries both
        # `key` and `token` in its query string — so a failed test answered
        # with the secret it was testing, straight into a toast in the browser,
        # into whatever logs the response, and into any screenshot of the page.
        # Found by pressing Test on a channel saved with the wrong kind.
        #
        # The status and the reason are what the operator needs; the address is
        # what they already have.
        detail = _redact_url(str(exc), row.webhook_url)
        logger.warning("channel_test_failed id=%s kind=%s", channel_id, row.kind)
        return {"ok": False, "detail": detail}
    return {"ok": True, "detail": "sent"}


def _redact_url(message: str, url: str) -> str:
    """Take a webhook URL, and anything that looks like one, out of a message.

    Both halves matter: the exact URL, because it is the one thing we know is
    a secret here, and the general shape, because a client library is free to
    quote a redirect target or a second address we never stored.
    """
    import re

    out = message.replace(url, "<webhook url>")
    if url:
        base = url.split("?", 1)[0]
        out = out.replace(base, "<webhook url>")
    # Any surviving query string on any URL: the secret parts live there.
    out = re.sub(r"(https?://[^\s'\"]+)\?[^\s'\"]*", r"\1?<redacted>", out)
    return out[:400]


class BindingIn(BaseModel):
    channel_id: str
    repo_slug: str | None = None
    event: str = Field(min_length=1, max_length=100)
    min_severity: str = Field(default="info", pattern="^(info|warn|warning|error|critical)$")
    enabled: bool = True
    model_config = ConfigDict(extra="forbid")


class BindingOut(BaseModel):
    id: str
    channel_id: str
    repo_slug: str | None
    event: str
    min_severity: str
    enabled: bool


@notif_router.get("/bindings", response_model=list[BindingOut])
async def list_bindings(
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[BindingOut]:
    # A binding carries no workspace of its own, but the channel it points at
    # does — so tenancy is a join, not a new column. Without this the endpoint
    # answered with every tenant's routing table: which repos they watch, which
    # events they care about, and the channel ids behind them.
    rows = (await session.scalars(
        select(ChannelBinding)
        .join(NotificationChannel, NotificationChannel.id == ChannelBinding.channel_id)
        .where(NotificationChannel.workspace_id == ws_id)
        .order_by(ChannelBinding.repo_slug, ChannelBinding.event)
    )).all()
    return [BindingOut(
        id=r.id, channel_id=r.channel_id, repo_slug=r.repo_slug,
        event=r.event, min_severity=r.min_severity, enabled=r.enabled,
    ) for r in rows]


@notif_router.post("/bindings", response_model=BindingOut, status_code=201)
async def create_binding(
    payload: BindingIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> BindingOut:
    channel = await session.get(NotificationChannel, payload.channel_id)
    if channel is None or channel.workspace_id != ws_id:
        # Binding a foreign channel would route this workspace's events into
        # another tenant's chat room.
        raise HTTPException(status_code=404, detail="channel not found")
    row = ChannelBinding(
        id=str(uuid.uuid4()),
        channel_id=payload.channel_id,
        repo_slug=payload.repo_slug,
        event=payload.event,
        min_severity=payload.min_severity,
        enabled=payload.enabled,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info("binding_created id=%s event=%s by=%s", row.id, row.event, user.email)
    return BindingOut(
        id=row.id, channel_id=row.channel_id, repo_slug=row.repo_slug,
        event=row.event, min_severity=row.min_severity, enabled=row.enabled,
    )


@notif_router.delete("/bindings/{binding_id}", status_code=204)
async def delete_binding(
    binding_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    row = await session.get(ChannelBinding, binding_id)
    if row is None:
        return
    channel = await session.get(NotificationChannel, row.channel_id)
    if channel is None or channel.workspace_id != ws_id:
        # Silent no-op, matching the missing-row branch above: a 404 here would
        # confirm that the id exists somewhere else.
        return
    await session.delete(row)
    await session.commit()
    logger.info("binding_deleted id=%s by=%s", binding_id, user.email)


__all__ = ["intel_router", "notif_router"]
