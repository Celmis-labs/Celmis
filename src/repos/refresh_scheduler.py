"""Daily: has the branch moved? If yes re-index, if no say so.

One asyncio task, the same shape as `src/ownership/scheduler.py` and the
review poller — no APScheduler, no extra dependency, next run scheduled after
the previous batch finishes so a slow pass cannot pile up on itself.

WHAT IT COSTS. One `git ls-remote` per repository per day: a single network
round trip, no clone, no fetch. Repositories that have not moved cost exactly
that and nothing else — no parse, no embedding, no model call. That is what
makes a daily sweep affordable on a box that also serves the product.

WHY A SWEEP AT ALL, given the webhook. A push webhook is immediate and
precise, and it exists — but it only fires where somebody registered it, and
it does not fire for a repository indexed from a branch nobody pushes to
through this instance. The sweep is the floor: every registered repository is
looked at once a day whatever else is or is not configured. The two are not
alternatives, and neither one alone leaves the index reliably current.

THE STAGGER IS NOT POLITENESS. Fifty repositories checked in the same second
is fifty concurrent git processes and fifty requests to one provider, which is
how a scheduled task earns a rate limit for the whole workspace.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_TASK: asyncio.Task | None = None

#: Once a day by default. Set to 0 to disable the sweep entirely — an operator
#: who drives indexing from webhooks alone should be able to say so, and
#: "disabled" is a supported configuration rather than something to achieve by
#: setting the interval absurdly high.
_ENV_INTERVAL = "CELMIS_REFRESH_INTERVAL_HOURS"
_ENV_STAGGER = "CELMIS_REFRESH_STAGGER_SECONDS"
_ENV_FIRST_DELAY = "CELMIS_REFRESH_FIRST_DELAY_SECONDS"


def _number(name: str, default: float) -> float:
    """A setting, or the default — never an exception.

    `_hours` was guarded and the other two were bare `float()` calls made
    INSIDE the task, before its loop. A typo in either
    (`CELMIS_REFRESH_STAGGER_SECONDS=5s`) would raise on the first tick, kill
    the task, and the sweep would simply never happen again — with the only
    trace an unhandled-task warning nobody reads. A daily job that silently
    stops is the failure this whole feature exists to prevent, one level up.
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("refresh_scheduler_bad_setting %s=%r — using %s",
                       name, raw, default)
        return default


def _hours() -> float:
    return _number(_ENV_INTERVAL, 24.0)


def start_refresh_scheduler() -> None:
    """Kick off the daily sweep. Idempotent; a zero interval disables it."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    if _hours() <= 0:
        logger.info("refresh_scheduler_disabled (%s=%s)",
                    _ENV_INTERVAL, os.environ.get(_ENV_INTERVAL))
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _TASK = loop.create_task(_run_forever())
    logger.info("refresh_scheduler_started interval_hours=%s", _hours())


def stop_refresh_scheduler() -> None:
    """For tests and shutdown."""
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
    _TASK = None


async def _run_forever() -> None:
    stagger = _number(_ENV_STAGGER, 5.0)
    # Startup already clones, migrates and warms caches; a sweep competing
    # with that would make a cold start look slow for no benefit.
    await asyncio.sleep(_number(_ENV_FIRST_DELAY, 120.0))
    while True:
        started = datetime.now(UTC)
        try:
            summary = await sweep_once(stagger=stagger)
            logger.info(
                "refresh_sweep_done checked=%d behind=%d up_to_date=%d "
                "unreachable=%d never_indexed=%d seconds=%.1f",
                summary["checked"], summary["behind"], summary["up_to_date"],
                summary["unreachable"], summary["never_indexed"],
                (datetime.now(UTC) - started).total_seconds(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("refresh_sweep_failed err=%s", exc)
        await asyncio.sleep(max(60.0, _hours() * 3600.0))


async def sweep_once(*, stagger: float = 5.0) -> dict[str, int]:
    """One pass over every registered repository in every workspace.

    Returns counts by outcome. Never raises for a single repository — one
    unreachable remote must not end the sweep for the rest, which is the
    difference between a scheduled task and a script.
    """
    from src.api.auto_review import get_auto_review_store
    from src.repos.freshness import check_repo

    store = get_auto_review_store()
    try:
        configs = list(store.list_all())
    except Exception as exc:  # noqa: BLE001
        logger.warning("refresh_sweep_list_failed err=%s", exc)
        return _empty_summary()

    summary = _empty_summary()
    for i, cfg in enumerate(configs):
        if i and stagger:
            await asyncio.sleep(stagger)
        workspace_id = getattr(cfg, "workspace_id", None) or "default"
        try:
            result = await asyncio.to_thread(
                check_repo, cfg.repo_slug,
                workspace_id=workspace_id,
                user_id=getattr(cfg, "user_id", "default") or "default",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("refresh_check_failed repo=%s err=%s", cfg.repo_slug, exc)
            summary["unreachable"] += 1
            summary["checked"] += 1
            continue
        summary["checked"] += 1
        summary[result.state] = summary.get(result.state, 0) + 1
    return summary


def _empty_summary() -> dict[str, int]:
    return {"checked": 0, "behind": 0, "up_to_date": 0,
            "unreachable": 0, "never_indexed": 0}


__all__ = ["start_refresh_scheduler", "stop_refresh_scheduler", "sweep_once"]
