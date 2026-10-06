"""What the review has learned from the team's feedback (/memories, Learning tab).

Endpoints:
    GET    /api/learning/summary        — counts, implementation rate, the filter's mode, what is dismissed most
    GET    /api/learning/signals        — the recorded signals (newest first, paged)
    DELETE /api/learning/signals/{id}   — forget one signal (and its vector)

Access is that of the memories page: editor, admin or owner of the active
workspace (or a global admin); viewers and members get 403 on every endpoint.
An editor sees the signals of the repositories a team of theirs grants `read`
on, plus nothing else; admins and owners see every repository of the
workspace. Forgetting one needs `review` on its repository. Every row is read
and written in the ACTIVE workspace only: an id of another workspace is a 404.

The people behind the signals are shown to workspace admins only; everybody
else sees what was said about a finding, not who said it. Nothing here returns
a secret or the text of a pull request beyond the finding's own title and path.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from src.api.deps import (
    current_workspace_id,
    enforce_repo_permission,
    is_workspace_admin,
    readable_repo_slugs,
    require_memories_access,
)
from src.api.routers.review_rules import _audit
from src.review.learning import signals as sig
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/learning", tags=["learning"])

MAX_PAGE = 100
TOP_DISMISSED = 10


def _scope_clause(model: Any, ws: str, repo: str | None, visible: list[str] | None):
    clause = [model.workspace_id == ws]
    if repo:
        clause.append(model.repo_slug == repo)
    elif visible is not None:
        clause.append(model.repo_slug.in_(visible))
    return clause


def _repos_with_signals(ws: str) -> list[str]:
    from src.db.models import FindingSignal as M

    with sig._session() as s:
        return [r for r in s.scalars(select(M.repo_slug).where(M.workspace_id == ws).distinct()) if r]


def _summary(ws: str, repo: str | None, visible: list[str] | None) -> dict:
    from src.db.models import FindingSignal as M
    from src.review.memories import _effective_setting
    from src.review.settings import get_review_settings

    days = int(get_review_settings().learning_window_days)
    since = sig.window_start(days)
    where = _scope_clause(M, ws, repo, visible) + [M.created_at >= since]
    with sig._session() as s:
        by_signal = {k: int(n) for k, n in s.execute(
            select(M.signal, func.count()).where(*where).group_by(M.signal))}
        by_source = {k: int(n) for k, n in s.execute(
            select(M.source, func.count()).where(*where).group_by(M.source))}
        dismissed = s.scalars(select(M).where(*where, M.signal == "dismissed").order_by(
            M.created_at.desc()).limit(1000)).all()
        groups: dict[str, dict] = {}
        prs: dict[str, set] = defaultdict(set)
        for r in dismissed:
            g = groups.setdefault(r.fingerprint, {
                "fingerprint": r.fingerprint, "title": r.title, "file_path": r.file_path,
                "rule_id": r.rule_id, "agent": r.agent, "repo_slug": r.repo_slug})
            prs[r.fingerprint].add((r.pr_provider, r.pr_repo, r.pr_number, r.actor))
            g["dismissals"] = len(prs[r.fingerprint])
        top = sorted(groups.values(), key=lambda g: (-g["dismissals"], g["title"]))[:TOP_DISMISSED]
        mode = str(_effective_setting(s, ws, repo, "learning_suppression") or "shadow")
    impl = sig.implementation_rate(ws, repo, since, visible=visible)
    return {
        "window_days": days,
        "mode": mode,
        "signals": by_signal,
        "sources": by_source,
        "total": sum(by_signal.values()),
        "implementation": impl,
        "top_dismissed": top,
    }


def _signals(ws: str, repo: str | None, visible: list[str] | None, signal: str | None,
             limit: int, offset: int, show_actor: bool) -> dict:
    from src.db.models import FindingSignal as M

    where = _scope_clause(M, ws, repo, visible)
    if signal:
        where.append(M.signal == signal)
    with sig._session() as s:
        total = int(s.scalar(select(func.count()).select_from(M).where(*where)) or 0)
        rows = s.scalars(select(M).where(*where).order_by(M.created_at.desc())
                         .limit(limit).offset(offset)).all()
        return {"total": total,
                "signals": [sig.signal_to_dict(r, show_actor=show_actor) for r in rows]}


async def _visible(user: User, ws: str, repo: str | None) -> list[str] | None:
    """None when one named repository is the scope (already checked) or the
    caller holds the whole workspace; otherwise the readable slugs."""
    if repo:
        await enforce_repo_permission(repo, user, "read", ws)
        return None
    if await asyncio.to_thread(is_workspace_admin, user, ws):
        return None
    slugs = await asyncio.to_thread(_repos_with_signals, ws)
    return sorted(await readable_repo_slugs(user, ws, slugs))


@router.get("/summary")
async def summary(
    repo: str | None = Query(default=None, max_length=300),
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """What the team's feedback taught, over the learning window."""
    repo_slug = (repo or "").strip() or None
    visible = await _visible(user, ws_id, repo_slug)
    return {"repo_slug": repo_slug,
            **await asyncio.to_thread(_summary, ws_id, repo_slug, visible)}


@router.get("/signals")
async def list_signals(
    repo: str | None = Query(default=None, max_length=300),
    signal: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=MAX_PAGE),
    offset: int = Query(default=0, ge=0, le=100_000),
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    if signal is not None and signal not in sig.SIGNALS:
        raise HTTPException(status_code=422, detail=f"signal must be one of {list(sig.SIGNALS)}")
    repo_slug = (repo or "").strip() or None
    visible = await _visible(user, ws_id, repo_slug)
    show_actor = bool(await asyncio.to_thread(is_workspace_admin, user, ws_id))
    out = await asyncio.to_thread(
        _signals, ws_id, repo_slug, visible, signal, limit, offset, show_actor)
    return {"repo_slug": repo_slug, "limit": limit, "offset": offset, **out}


@router.delete("/signals/{signal_id}", status_code=204)
async def forget(
    signal_id: str,
    request: Request,
    user: User = Depends(require_memories_access),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    """Forget one signal: it stops counting for the filter and the rules job."""
    from src.db.models import FindingSignal as M

    def lookup() -> str | None:
        with sig._session() as s:
            row = s.scalars(select(M).where(
                M.id == signal_id, M.workspace_id == ws_id).limit(1)).first()
            return row.repo_slug if row is not None else None

    slug = await asyncio.to_thread(lookup)
    if slug is None:
        raise HTTPException(status_code=404, detail="No such signal in this workspace")
    seen = await readable_repo_slugs(user, ws_id, [slug])
    if slug not in seen:
        raise HTTPException(status_code=404, detail="No such signal in this workspace")
    await enforce_repo_permission(slug, user, "review", ws_id)
    gone = await asyncio.to_thread(sig.forget_signal, ws_id, signal_id)
    if gone is None:
        raise HTTPException(status_code=404, detail="No such signal in this workspace")
    _audit("learning.signal_forgotten", user=user, ws_id=ws_id, target=slug, request=request,
           detail={"signal": gone["signal"], "source": gone["source"]})
