"""Workspace LLM spend ledger + budget enforcement (Stage 23).

Every LLM call — chat answers, PR-review agents, embeddings — writes one row to
``llm_spend``. That gives a single authoritative source for:

  * the admin Usage view (tokens & cost by surface / agent / model, incl.
    cached-input tokens and whether the cost is a real charge or an estimate)
  * a per-workspace monthly cap that can *block* further calls.

Design notes:

  * Recording never raises. A ledger failure must not break generation.
  * Enforcement is opt-in per workspace: no budget row, or ``monthly_usd_cap``
    of 0, means unlimited (the default), so existing installs are unaffected.
  * Only ``hard_stop`` budgets refuse calls; otherwise we merely report that
    the workspace is over its alert threshold.

"No row" and "the row could not be read" are different facts that used to
produce the same answer — unlimited. A database that is down, or a model that
no longer matches its table, therefore *removed* every cap in the installation,
which is the direction a spend limit must never fail in. Under multi_tenant
(:mod:`src.deployment`) an unreadable budget raises :class:`BudgetUnavailable`
instead; under single_tenant it still reads as unlimited, because a one-tenant
box would rather answer the question than protect a cap from its own operator.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Surfaces (kept as constants so the UI and the writers agree on spelling).
# The Usage view groups by whatever string lands in ``llm_spend.surface`` — there
# is no whitelist — so the only rule is: one concept, one spelling.
SURFACE_QA = "qa"
SURFACE_REVIEW = "review"
SURFACE_EMBEDDINGS = "embeddings"
#: Alias — writers that think in the singular must NOT invent a second bucket,
#: otherwise the same concept splits into two rows in ``by_surface``.
SURFACE_EMBEDDING = SURFACE_EMBEDDINGS
SURFACE_VAULT = "vault"          # batch doc generation (module PRD / feature / integration)
SURFACE_AGENT = "agent"          # Claude Code agent sessions (subscription)
SURFACE_DEPS = "deps"            # dependency-report generation
#: The Celmis agent's planner — reading a sentence into a plan. It booked to
#: `qa` until now, which put it in the same bucket as chat: the two are asked
#: the same way and cost nothing alike, and a workspace could not see which of
#: them it was paying for.
SURFACE_AUTOMATION = "automation"
SURFACE_OTHER = "other"          # unclassified — better than mislabelling


class BudgetExceeded(RuntimeError):
    """Raised when a hard-stop workspace budget is exhausted."""

    def __init__(self, workspace_id: str, spent: float, cap: float) -> None:
        self.workspace_id = workspace_id
        self.spent = spent
        self.cap = cap
        super().__init__(
            f"workspace {workspace_id!r} has spent ${spent:.2f} of its "
            f"${cap:.2f} monthly LLM budget — further calls are blocked. "
            f"Raise the cap in Settings → Usage."
        )


class BudgetUnavailable(BudgetExceeded):
    """The budget could not be read and this installation will not guess.

    A *subclass* of :class:`BudgetExceeded` on purpose: every caller already
    handles that one (``src/llm/errors.py`` classifies it, the Q&A stream turns
    it into a ``budget_exceeded`` event), so refusing here surfaces as a clean
    "blocked" message rather than a 500 nobody can read.
    """

    def __init__(self, workspace_id: str, reason: str) -> None:
        self.workspace_id = workspace_id
        self.spent = 0.0
        self.cap = 0.0
        self.reason = reason
        RuntimeError.__init__(
            self,
            f"the LLM budget for workspace {workspace_id!r} could not be read "
            f"({reason}); this installation runs in multi_tenant mode, where an "
            f"unreadable cap blocks the call instead of removing the cap.",
        )


@dataclass(frozen=True)
class BudgetStatus:
    workspace_id: str
    cap_usd: float
    spent_usd: float
    hard_stop: bool
    alert_pct: int

    @property
    def enabled(self) -> bool:
        return self.cap_usd > 0

    @property
    def used_pct(self) -> float:
        if not self.enabled:
            return 0.0
        return round(self.spent_usd / self.cap_usd * 100, 1)

    @property
    def over_cap(self) -> bool:
        return self.enabled and self.spent_usd >= self.cap_usd

    @property
    def over_alert(self) -> bool:
        return self.enabled and self.used_pct >= self.alert_pct

    def as_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "cap_usd": self.cap_usd,
            "spent_usd": round(self.spent_usd, 4),
            "used_pct": self.used_pct,
            "hard_stop": self.hard_stop,
            "alert_pct": self.alert_pct,
            "enabled": self.enabled,
            "over_cap": self.over_cap,
            "over_alert": self.over_alert,
        }


_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def _engine():
    """One process-wide engine, built lazily.

    The ledger is now written from the embedding path too (one row per batch,
    i.e. thousands of rows per index run) — building a fresh ``Engine`` with its
    own pool per row would mean a new TCP connect + auth per LLM call, which
    would dominate the cost of the thing being measured.
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            from sqlalchemy import create_engine

            from src.db.session import get_database_url

            url = get_database_url().replace(
                "postgresql+asyncpg://", "postgresql+psycopg://",
            )
            _ENGINE = create_engine(
                url, pool_pre_ping=True, pool_size=2, max_overflow=5,
                pool_recycle=1800,
            )
    return _ENGINE


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def record_spend(
    *,
    workspace_id: str = "default",
    surface: str,
    model: str = "",
    provider: str = "",
    cost_usd: float = 0.0,
    cost_source: str = "unknown",
    tokens_in: int = 0,
    tokens_out: int = 0,
    cached_tokens_in: int = 0,
    agent: str | None = None,
    user_id: str | None = None,
    repo_slug: str | None = None,
    operation: str | None = None,
) -> None:
    """Append one spend row. Never raises — a ledger write must not be able to
    fail a user-facing generation."""
    try:
        from sqlalchemy.orm import Session

        from src.db.models import LlmSpend

        with Session(_engine()) as s:
            s.add(LlmSpend(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id or "default",
                surface=surface,
                agent=agent,
                model=model or "",
                provider=provider or "",
                cost_usd=float(cost_usd or 0.0),
                cost_source=cost_source or "unknown",
                tokens_in=int(tokens_in or 0),
                tokens_out=int(tokens_out or 0),
                cached_tokens_in=int(cached_tokens_in or 0),
                user_id=user_id,
                repo_slug=repo_slug,
                operation=operation,
            ))
            s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_spend_record_failed surface=%s err=%s", surface, exc)


def month_spend(workspace_id: str = "default") -> float:
    """Total USD spent by this workspace since the start of the current month."""
    try:
        from sqlalchemy import func as sql_func
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from src.db.models import LlmSpend

        with Session(_engine()) as s:
            total = s.execute(
                select(sql_func.coalesce(sql_func.sum(LlmSpend.cost_usd), 0.0))
                .where(
                    LlmSpend.workspace_id == workspace_id,
                    LlmSpend.created_at >= _month_start(),
                )
            ).scalar()
            return float(total or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("month_spend_failed ws=%s err=%s", workspace_id, exc)
        from src.deployment import fall_open_allowed
        if fall_open_allowed("llm.budget.unreadable",
                             detail=f"ws={workspace_id} query=month_spend"):
            return 0.0
        raise BudgetUnavailable(workspace_id, f"spend query failed: {exc}") from exc


def get_status(workspace_id: str = "default") -> BudgetStatus:
    """Current budget position. Unlimited when no row / cap is 0."""
    cap, hard_stop, alert_pct = 0.0, False, 80
    try:
        from sqlalchemy.orm import Session

        from src.db.models import WorkspaceBudget

        with Session(_engine()) as s:
            row = s.get(WorkspaceBudget, workspace_id)
            if row is not None:
                cap = float(row.monthly_usd_cap or 0.0)
                hard_stop = bool(row.hard_stop)
                alert_pct = int(row.alert_pct or 80)
    except Exception as exc:  # noqa: BLE001
        logger.warning("budget_load_failed ws=%s err=%s", workspace_id, exc)
        from src.deployment import fall_open_allowed
        if not fall_open_allowed("llm.budget.unreadable",
                                 detail=f"ws={workspace_id} query=budget_row"):
            raise BudgetUnavailable(workspace_id, f"budget row unreadable: {exc}") from exc

    return BudgetStatus(
        workspace_id=workspace_id, cap_usd=cap,
        spent_usd=month_spend(workspace_id) if cap > 0 else 0.0,
        hard_stop=hard_stop, alert_pct=alert_pct,
    )


def enforce(workspace_id: str = "default") -> BudgetStatus:
    """Check the budget before an LLM call.

    Raises :class:`BudgetExceeded` only for hard-stop budgets that are over the
    cap; otherwise returns the status so callers can surface a warning.
    """
    status = get_status(workspace_id)
    if status.over_cap and status.hard_stop:
        logger.warning(
            "budget_block ws=%s spent=%.2f cap=%.2f",
            workspace_id, status.spent_usd, status.cap_usd,
        )
        raise BudgetExceeded(workspace_id, status.spent_usd, status.cap_usd)
    if status.over_alert:
        logger.info(
            "budget_alert ws=%s used=%.1f%% cap=%.2f",
            workspace_id, status.used_pct, status.cap_usd,
        )
    return status


__all__ = [
    "BudgetExceeded", "BudgetStatus", "BudgetUnavailable",
    "SURFACE_QA", "SURFACE_REVIEW",
    "SURFACE_EMBEDDINGS", "SURFACE_EMBEDDING", "SURFACE_VAULT", "SURFACE_AGENT",
    "SURFACE_DEPS", "SURFACE_OTHER",
    "enforce", "get_status", "month_spend", "record_spend",
]
