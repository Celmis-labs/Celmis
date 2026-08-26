"""Admin Usage view + workspace budgets (Stage 23).

Reads the ``llm_spend`` ledger written by every LLM surface, so a tech-lead can
answer: where did the tokens go — Q&A chat, PR-review agents (and which agent),
or embeddings? Which models cost the most? How much of the prompt was served
from cache?

    GET  /api/spend/summary   — totals + every breakdown, over any window
    GET  /api/spend/daily     — a series, bucketed by hour/day/week/month

Every read takes the same window and the same filters, so a question asked of
the summary can be asked of the series without rephrasing it: `?surface=review`
answers "what did PR review cost", `&repo=acme-frontend` narrows it to one
repository, `&operation=integration_doc` to one kind of job inside it.

A window is either `days=N` or an explicit `since`/`until` pair. The explicit
pair wins when both are given — a caller that names both dates means them.
    GET  /api/spend/budget            — current cap + month-to-date position
    PUT  /api/spend/budget            — set the cap (admin)

Cost caveat surfaced to the UI: `cost_source` tells whether a figure is a real
provider charge (OpenRouter reports actuals) or a LiteLLM price-table estimate.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    current_workspace_id,
    get_current_user,
    require_workspace_admin,
)
from src.db.models import LlmSpend, WorkspaceBudget
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/spend", tags=["spend"])


class GroupRow(BaseModel):
    key: str
    #: What to SHOW instead of the key, when the key is an internal id.
    #: A uuid in a table headed "Who asked" answers nobody's question, and
    #: the reader cannot even tell whether two rows are two people.
    label: str | None = None
    calls: int
    tokens_in: int
    tokens_out: int
    cached_tokens_in: int
    cost_usd: float


class SpendSummary(BaseModel):
    days: int
    calls: int
    tokens_in: int
    tokens_out: int
    cached_tokens_in: int
    cost_usd: float
    cache_hit_pct: float                 # cached_in / (tokens_in + cached_in)
    estimated_share_pct: float           # share of cost that is an estimate
    # Calls billed by a subscription rather than per token — Claude Code agent
    # sessions. Their cost is genuinely 0, and without saying so a card
    # reading "12 calls, 193K tokens, $0.00" looks like broken accounting.
    subscription_calls: int = 0
    # The same totals split by HOW they are paid for, which is the split that
    # actually means something to whoever pays: a Claude subscription is a
    # flat monthly seat, a BYOK provider key is metered per token. Summing
    # them into one "cost" answers no real question.
    #   key="subscription" — Claude Code sessions on the connected account
    #   key="api_key"      — everything running on a workspace provider key
    by_billing: list[GroupRow] = []
    by_surface: list[GroupRow]
    by_agent: list[GroupRow]
    by_model: list[GroupRow]
    by_provider: list[GroupRow]
    #: Which repository the tokens went to. Empty for surfaces that do not
    #: work on one repository at a time — the row keyed "—" is not missing
    #: data, it is embeddings and chat, which span the workspace.
    by_repo: list[GroupRow] = []
    #: Which JOB inside the surface: module_prd vs feature_doc vs
    #: integration_doc, automation_interpret, deps_report. The finest cut
    #: there is, and the one that answers "why was the vault so expensive".
    by_operation: list[GroupRow] = []
    #: Who asked. A shared workspace splitting a bill needs this, and it is
    #: the only breakdown that is about people rather than machinery.
    by_user: list[GroupRow] = []
    #: The window actually used, echoed back so a page showing "last 30 days"
    #: cannot disagree with the numbers under it.
    since: str = ""
    until: str = ""


class DailyPoint(BaseModel):
    date: str
    cost_usd: float
    tokens_in: int
    tokens_out: int
    calls: int


class BudgetOut(BaseModel):
    workspace_id: str
    cap_usd: float
    spent_usd: float
    used_pct: float
    hard_stop: bool
    alert_pct: int
    enabled: bool
    over_cap: bool
    over_alert: bool


class BudgetIn(BaseModel):
    monthly_usd_cap: float = Field(default=0, ge=0, le=1_000_000)
    alert_pct: int = Field(default=80, ge=1, le=100)
    hard_stop: bool = False


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=max(1, days))


def _parse_ts(value: str | None) -> datetime | None:
    """An ISO date or datetime, or None. Never raises on rubbish input.

    A malformed date in a query string is a bookmark somebody edited by hand,
    not an attack; falling back to the default window shows them something
    rather than a 422 they cannot act on.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _window(days: int, since: str | None, until: str | None
            ) -> tuple[datetime, datetime]:
    """The range every read shares. Explicit dates win over `days`."""
    end = _parse_ts(until) or datetime.now(UTC)
    start = _parse_ts(since) or (end - timedelta(days=max(1, days)))
    if start > end:
        start, end = end, start
    return start, end


def _filters(ws: str, start: datetime, end: datetime, *, surface: str | None,
             repo: str | None, model: str | None, operation: str | None,
             agent: str | None) -> list:
    """The WHERE every query shares.

    Written once because a filter that applies to the totals and not to a
    breakdown produces a page whose columns do not add up to its header — the
    kind of wrong that looks like a rounding bug for a week.
    """
    where = [LlmSpend.workspace_id == ws,
             LlmSpend.created_at >= start, LlmSpend.created_at <= end]
    if surface:
        where.append(LlmSpend.surface == surface)
    if repo:
        where.append(LlmSpend.repo_slug == repo)
    if model:
        where.append(LlmSpend.model == model)
    if operation:
        where.append(LlmSpend.operation == operation)
    if agent:
        where.append(LlmSpend.agent == agent)
    return where


async def _grouped_billing(
    session: AsyncSession, where: list,
) -> list[GroupRow]:
    """Two rows: subscription vs metered API key.

    `cost_source` carries more detail than that (openrouter_actual,
    litellm_estimate, unknown), but the distinction a reader needs first is
    whether a number can grow their bill. Everything that is not a
    subscription is metered, whatever estimated it.
    """
    kind = case(
        (LlmSpend.cost_source == "subscription", literal("subscription")),
        else_=literal("api_key"),
    ).label("billing")
    rows = (await session.execute(
        select(
            kind,
            func.count().label("calls"),
            func.coalesce(func.sum(LlmSpend.tokens_in), 0),
            func.coalesce(func.sum(LlmSpend.tokens_out), 0),
            func.coalesce(func.sum(LlmSpend.cached_tokens_in), 0),
            func.coalesce(func.sum(LlmSpend.cost_usd), 0.0),
        )
        .where(*where)
        .group_by(kind)
        .order_by(func.coalesce(func.sum(LlmSpend.cost_usd), 0.0).desc())
    )).all()
    return [
        GroupRow(
            key=str(r[0]), calls=int(r[1]), tokens_in=int(r[2]),
            tokens_out=int(r[3]), cached_tokens_in=int(r[4]),
            cost_usd=round(float(r[5]), 4),
        )
        for r in rows
    ]


async def _grouped(
    session: AsyncSession, where: list, column, *, limit: int = 50,
) -> list[GroupRow]:
    """One breakdown. Capped: a workspace with two thousand repositories must
    not answer a summary request with two thousand rows nobody scrolls."""
    rows = (await session.execute(
        select(
            column,
            func.count().label("calls"),
            func.coalesce(func.sum(LlmSpend.tokens_in), 0),
            func.coalesce(func.sum(LlmSpend.tokens_out), 0),
            func.coalesce(func.sum(LlmSpend.cached_tokens_in), 0),
            func.coalesce(func.sum(LlmSpend.cost_usd), 0.0),
        )
        .where(*where)
        .group_by(column)
        # Cost first, then tokens: at the volumes where cost is still $0.00
        # because a model is free or subscription-billed, cost alone would
        # order the table arbitrarily.
        .order_by(func.coalesce(func.sum(LlmSpend.cost_usd), 0.0).desc(),
                  func.coalesce(func.sum(LlmSpend.tokens_out), 0).desc())
        .limit(limit)
    )).all()
    return [
        GroupRow(
            key=str(r[0]) if r[0] is not None else "—",
            calls=int(r[1]), tokens_in=int(r[2]), tokens_out=int(r[3]),
            cached_tokens_in=int(r[4]), cost_usd=round(float(r[5]), 4),
        )
        for r in rows
    ]


async def _name_users(session: AsyncSession, rows: list[GroupRow]) -> list[GroupRow]:
    """Put a person's name on each row of the by-user breakdown.

    Resolved here rather than stored on the ledger: a name changes, and a
    ledger that copied it at write time would show the old one forever. The
    id stays the key — clicking a row still filters by it — and the name is
    only what is drawn.
    """
    ids = [r.key for r in rows if r.key and r.key != "—"]
    if not ids:
        return rows
    try:
        from src.users.store import get_user_store

        store = get_user_store()
        for row in rows:
            user = store.get_by_id(row.key) if row.key != "—" else None
            if user is not None:
                row.label = user.name or user.email
    except Exception:  # noqa: BLE001 — a missing name must not empty the table
        logger.warning("spend_user_names_unavailable", exc_info=True)
    return rows


@router.get("/summary", response_model=SpendSummary)
async def summary(
    days: int = Query(default=30, ge=1, le=365),
    since: str | None = Query(default=None, description="ISO date/datetime"),
    until: str | None = Query(default=None, description="ISO date/datetime"),
    surface: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    model: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    # How many rows per breakdown. Capped at 50 by default so a summary stays
    # a summary; raisable because a repository ranked 51st was otherwise
    # unreachable — it could not be clicked into existence at all, and the
    # only recourse was narrowing the window until it climbed into view.
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_async_session),
    # Workspace-scoped read: any member sees THEIR active workspace's spend
    # (the query below already filters by ws) — /settings and /admin/usage
    # now show the same numbers. Budget writes stay admin-only.
    _user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> SpendSummary:
    start_at, end_at = _window(days, since, until)
    where = _filters(ws, start_at, end_at, surface=surface, repo=repo,
                     model=model, operation=operation, agent=agent)

    totals = (await session.execute(
        select(
            func.count(),
            func.coalesce(func.sum(LlmSpend.tokens_in), 0),
            func.coalesce(func.sum(LlmSpend.tokens_out), 0),
            func.coalesce(func.sum(LlmSpend.cached_tokens_in), 0),
            func.coalesce(func.sum(LlmSpend.cost_usd), 0.0),
        ).where(*where)
    )).one()
    calls, t_in, t_out, t_cached, cost = (
        int(totals[0]), int(totals[1]), int(totals[2]), int(totals[3]), float(totals[4]),
    )

    # How much of the reported cost is an estimate rather than a real charge.
    est = (await session.execute(
        select(func.coalesce(func.sum(LlmSpend.cost_usd), 0.0))
        .where(*where, LlmSpend.cost_source != "openrouter_actual")
    )).scalar() or 0.0

    sub_calls = int((await session.execute(
        select(func.count()).select_from(LlmSpend)
        .where(*where, LlmSpend.cost_source == "subscription")
    )).scalar() or 0)

    return SpendSummary(
        days=days, calls=calls, subscription_calls=sub_calls, tokens_in=t_in,
        tokens_out=t_out, cached_tokens_in=t_cached, cost_usd=round(cost, 4),
        since=start_at.isoformat(), until=end_at.isoformat(),
        # Denominator is total input, not the uncached remainder. Providers
        # report `input_tokens` EXCLUDING cache reads, so dividing by it gave
        # numbers like 1123518% the moment a prompt was mostly cached.
        cache_hit_pct=(round(t_cached / (t_in + t_cached) * 100, 1)
                       if (t_in + t_cached) else 0.0),
        estimated_share_pct=round(float(est) / cost * 100, 1) if cost else 0.0,
        by_billing=await _grouped_billing(session, where),
        by_surface=await _grouped(session, where, LlmSpend.surface, limit=limit),
        by_agent=await _grouped(session, where, LlmSpend.agent, limit=limit),
        by_model=await _grouped(session, where, LlmSpend.model, limit=limit),
        by_provider=await _grouped(session, where, LlmSpend.provider, limit=limit),
        by_repo=await _grouped(session, where, LlmSpend.repo_slug, limit=limit),
        by_operation=await _grouped(session, where, LlmSpend.operation, limit=limit),
        by_user=await _name_users(
            session, await _grouped(session, where, LlmSpend.user_id, limit=limit)),
    )


#: How a series is bucketed. Hour exists for the question "what happened
#: during that run this afternoon", which a daily bar cannot answer at all.
_BUCKETS = {"hour": "hour", "day": "day", "week": "week", "month": "month"}


@router.get("/daily", response_model=list[DailyPoint])
async def daily(
    days: int = Query(default=30, ge=1, le=365),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    bucket: str = Query(default="day"),
    surface: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    model: str | None = Query(default=None),
    operation: str | None = Query(default=None),
    agent: str | None = Query(default=None),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> list[DailyPoint]:
    """The same window and filters as the summary, over time.

    Kept at /daily rather than renamed: the path is in a shipped client, and
    a rename would buy a tidier URL at the price of a blank chart for anyone
    on the old page.
    """
    start_at, end_at = _window(days, since, until)
    where = _filters(ws, start_at, end_at, surface=surface, repo=repo,
                     model=model, operation=operation, agent=agent)
    unit = _BUCKETS.get(bucket, "day")
    slot = func.date_trunc(unit, LlmSpend.created_at).label("d")
    rows = (await session.execute(
        select(
            slot,
            func.coalesce(func.sum(LlmSpend.cost_usd), 0.0),
            func.coalesce(func.sum(LlmSpend.tokens_in), 0),
            func.coalesce(func.sum(LlmSpend.tokens_out), 0),
            func.count(),
        )
        .where(*where)
        .group_by(slot).order_by(slot)
    )).all()
    return [
        DailyPoint(
            # An hourly bucket needs the hour in the label; a daily one does
            # not, and printing midnight beside every date reads as noise.
            date=(r[0].isoformat() if unit == "hour" else r[0].date().isoformat()),
            cost_usd=round(float(r[1]), 4),
            tokens_in=int(r[2]), tokens_out=int(r[3]), calls=int(r[4]),
        )
        for r in rows
    ]


@router.get("/budget", response_model=BudgetOut)
async def get_budget(
    _user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> BudgetOut:
    from src.llm.budget import get_status
    return BudgetOut(**get_status(ws).as_dict())


@router.put("/budget", response_model=BudgetOut)
async def put_budget(
    payload: BudgetIn,
    session: AsyncSession = Depends(get_async_session),
    # Owner or admin OF THIS WORKSPACE, not a global admin: a cap on what a
    # workspace may spend belongs to whoever pays for it, and the person who
    # chooses the models could not previously cap what they cost.
    admin: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> BudgetOut:
    row = await session.get(WorkspaceBudget, ws)
    if row is None:
        row = WorkspaceBudget(workspace_id=ws)
        session.add(row)
    row.monthly_usd_cap = float(payload.monthly_usd_cap)
    row.alert_pct = int(payload.alert_pct)
    row.hard_stop = bool(payload.hard_stop)
    row.updated_by = admin.email
    await session.commit()
    logger.info(
        "budget_saved ws=%s cap=%.2f hard_stop=%s by=%s",
        ws, payload.monthly_usd_cap, payload.hard_stop, admin.email,
    )
    from src.llm.budget import get_status
    return BudgetOut(**get_status(ws).as_dict())
