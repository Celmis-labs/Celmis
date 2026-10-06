"""The hourly tick: queue one sync job per enabled repository.

The refresh scheduler's shape (`src/repos/refresh_scheduler.py`): one asyncio
task, the next tick scheduled after the previous one finishes, nothing
imported at module level that could fail the API's start.

The tick does no provider work itself. It reads the settings once, and for each
registered repository whose `enabled` resolves to true queues
`KIND_PRODUCTIVITY_SYNC` with a dedup key, so a slice that is still running (or
still waiting) is not queued twice. A fresh install, with nothing enabled,
queues nothing and spends nothing.

Environment:
    CELMIS_PRODUCTIVITY_INTERVAL_MINUTES   60; 0 turns the tick off
    CELMIS_DISABLE_PRODUCTIVITY_SCHED=1    (read by api/main.py) same, hard
    CELMIS_PRODUCTIVITY_FIRST_DELAY_SECONDS  180
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_TASK: asyncio.Task | None = None

_ENV_INTERVAL = "CELMIS_PRODUCTIVITY_INTERVAL_MINUTES"
_ENV_FIRST_DELAY = "CELMIS_PRODUCTIVITY_FIRST_DELAY_SECONDS"


def _number(name: str, default: float) -> float:
    """A setting or the default, never an exception: a typo must not kill the tick for good."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("productivity_scheduler_bad_setting %s=%r — using %s", name, raw, default)
        return default


def _minutes() -> float:
    return _number(_ENV_INTERVAL, 60.0)


def start_productivity_scheduler() -> None:
    """Idempotent; a zero interval disables it."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    if _minutes() <= 0:
        logger.info("productivity_scheduler_disabled (%s=%s)", _ENV_INTERVAL, os.environ.get(_ENV_INTERVAL))
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _TASK = loop.create_task(_run_forever())
    logger.info("productivity_scheduler_started interval_minutes=%s", _minutes())


def stop_productivity_scheduler() -> None:
    """For tests and shutdown."""
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
    _TASK = None


async def _run_forever() -> None:
    # Startup already clones, migrates and warms caches; a tick competing with
    # that would make a cold start look slow for nothing.
    await asyncio.sleep(_number(_ENV_FIRST_DELAY, 180.0))
    while True:
        try:
            queued = await asyncio.to_thread(tick_once)
            logger.info("productivity_tick queued=%d", queued)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("productivity_tick_failed err=%s", exc)
        await asyncio.sleep(max(60.0, _minutes() * 60.0))


def tick_once(*, engine=None, store=None, enqueue=None) -> int:
    """Queue a sync job for every enabled repository. Returns how many were queued."""
    from src.api.auto_review import get_auto_review_store
    from src.productivity import settings as settings_mod
    from src.productivity.sync import enqueue_sync

    index = settings_mod.load_index(engine)
    if not any(row.get("enabled") for row in index.rows.values()):
        return 0
    queued = 0
    for cfg in (store or get_auto_review_store()).list_all():
        workspace_id = getattr(cfg, "workspace_id", None) or "default"
        if not index.for_repo(workspace_id, cfg.provider, cfg.full_name).enabled:
            continue
        try:
            job = enqueue_sync(
                workspace_id, cfg.provider, cfg.full_name,
                user_id=getattr(cfg, "user_id", "default") or "default",
                repo_slug=cfg.repo_slug, enqueue=enqueue)
        except Exception as exc:  # noqa: BLE001 — one bad repo must not end the tick
            logger.warning("productivity_enqueue_failed repo=%s err=%s", cfg.full_name, exc)
            continue
        queued += 1 if job else 0
    return queued


__all__ = ["start_productivity_scheduler", "stop_productivity_scheduler", "tick_once"]
