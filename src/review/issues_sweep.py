"""Daily: is anything on the issues backlog fixed by now?

The merge webhook rechecks a branch right after a merge; this is the floor
under it. A fix can land through a push nobody told us about (a direct
commit, a branch we have no webhook on, a merge while we were down), and the
backlog must not wait for the next PR to notice it.

One asyncio task, the same shape as `repos.refresh_scheduler`: no extra
dependency, the next run is scheduled after the previous pass finished.

WHAT IT COSTS. One branch-head request per (repository, branch) that has
backlog issues; when the head is the one the last complete pass stopped at,
that is all (`recheck_backlog` reads the head first and skips what it has
already judged). The model is only asked about files that changed, within
each repository's `issues_resolve_max_llm`.

CELMIS_ISSUES_SWEEP_INTERVAL_HOURS=0 turns the sweep off (default 24).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

_TASK: asyncio.Task | None = None

_ENV_INTERVAL = "CELMIS_ISSUES_SWEEP_INTERVAL_HOURS"
_ENV_STAGGER = "CELMIS_ISSUES_SWEEP_STAGGER_SECONDS"
_ENV_FIRST_DELAY = "CELMIS_ISSUES_SWEEP_FIRST_DELAY_SECONDS"


def _number(name: str, default: float) -> float:
    """A setting, or the default — never an exception (a typo must not kill a
    daily task before its first tick)."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("issues_sweep_bad_setting %s=%r — using %s", name, raw, default)
        return default


def _hours() -> float:
    return _number(_ENV_INTERVAL, 24.0)


def start_issues_sweep() -> None:
    """Kick off the daily sweep. Idempotent; a zero interval disables it."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    if _hours() <= 0:
        logger.info("issues_sweep_disabled (%s=%s)", _ENV_INTERVAL,
                    os.environ.get(_ENV_INTERVAL))
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _TASK = loop.create_task(_run_forever())
    logger.info("issues_sweep_started interval_hours=%s", _hours())


def stop_issues_sweep() -> None:
    """For tests and shutdown."""
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
    _TASK = None


async def _run_forever() -> None:
    stagger = _number(_ENV_STAGGER, 5.0)
    await asyncio.sleep(_number(_ENV_FIRST_DELAY, 300.0))
    while True:
        started = datetime.now(UTC)
        try:
            summary = await sweep_once(stagger=stagger)
            logger.info("issues_sweep_done seconds=%.1f %s",
                        (datetime.now(UTC) - started).total_seconds(),
                        " ".join(f"{k}={v}" for k, v in summary.items()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("issues_sweep_failed err=%s", exc)
        await asyncio.sleep(max(60.0, _hours() * 3600.0))


def backlog_branches(engine=None) -> list[tuple[str, str, str, str]]:
    """Every (workspace, provider, repo, base branch) with something to watch:
    an open merged-PR issue, or an auto-fixed one (a revert would reopen it)."""
    from sqlalchemy import and_, or_, select
    from sqlalchemy.orm import Session

    from src.db.models import ReviewIssue
    from src.review import issues as ledger
    from src.review.issue_resolver import REVERT_WATCH_DAYS
    from src.review.issues import AUTO_RESOLUTION_SOURCES

    cutoff = datetime.now(UTC) - timedelta(days=REVERT_WATCH_DAYS)
    with Session(engine or ledger._engine()) as s:
        # Repeats whose canonical issue is closed would never be listed.
        if ledger.release_orphan_dups(s, None):
            s.commit()
        rows = s.execute(
            select(ReviewIssue.workspace_id, ReviewIssue.pr_provider,
                   ReviewIssue.pr_repo, ReviewIssue.base_ref)
            .where(
                ReviewIssue.merged_at.is_not(None),
                ReviewIssue.base_ref.is_not(None),
                ReviewIssue.dup_of.is_(None),
                or_(ReviewIssue.status == "open",
                    and_(ReviewIssue.status == "fixed",
                         ReviewIssue.resolution_source.in_(AUTO_RESOLUTION_SOURCES),
                         or_(ReviewIssue.closed_at.is_(None),
                             ReviewIssue.closed_at >= cutoff))),
            ).distinct().order_by(ReviewIssue.workspace_id, ReviewIssue.pr_repo,
                      ReviewIssue.base_ref)
        ).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


async def sweep_once(*, stagger: float = 5.0, engine=None) -> dict[str, int]:
    """One pass over every branch with a backlog. Never raises for one branch:
    an unreadable remote must not end the sweep for the rest."""
    from src.review.issue_resolver import recheck_backlog

    summary = {"branches": 0, "done": 0, "unreadable": 0, "busy": 0,
               "disabled": 0, "nothing": 0, "resolved": 0, "reopened": 0}
    try:
        branches = await asyncio.to_thread(backlog_branches, engine)
    except Exception as exc:  # noqa: BLE001
        logger.warning("issues_sweep_list_failed err_type=%s", type(exc).__name__)
        return summary
    for i, (ws, prov, repo, base) in enumerate(branches):
        if i and stagger:
            await asyncio.sleep(stagger)
        summary["branches"] += 1
        try:
            res = await asyncio.to_thread(
                recheck_backlog, ws, prov, repo, base, reason="sweep", engine=engine)
        except Exception as exc:  # noqa: BLE001
            logger.warning("issues_sweep_branch_failed repo=%s err_type=%s",
                           repo, type(exc).__name__)
            summary["unreadable"] += 1
            continue
        summary[res.status] = summary.get(res.status, 0) + 1
        summary["resolved"] += int(res.counts.get("resolved", 0))
        summary["reopened"] += int(res.counts.get("reopened", 0))
    return summary


__all__ = ["backlog_branches", "start_issues_sweep", "stop_issues_sweep", "sweep_once"]
