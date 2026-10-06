"""The weekly rules-from-history tick.

Off unless `learning_rules_schedule` is `weekly` (an install setting; the
built-in is off, so nothing here costs anything until somebody asks). Every few
hours the loop looks for repositories with enough recent signals to say
something, and for each one without a "history" rules job in the last week
starts one. The proposals land pending (origin "learned") — the tick never
changes a review by itself.

`CELMIS_DISABLE_LEARNING_SCHED=1` turns the loop off entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

_TASK: asyncio.Task | None = None
CHECK_EVERY_SECONDS = 6 * 3600
FIRST_DELAY_SECONDS = 600
#: A repository is proposed for at most once per this long.
MIN_GAP = timedelta(days=6, hours=12)
#: Repositories handled by one pass (a pass runs jobs one after another).
MAX_PER_PASS = 20


def start_learning_scheduler() -> None:
    """Kick off the background loop. Idempotent."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    if os.environ.get("CELMIS_DISABLE_LEARNING_SCHED", "").strip() == "1":
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _TASK = loop.create_task(_run_forever())
    logger.info("learning_scheduler_started")


def stop_learning_scheduler() -> None:
    """For tests and shutdown."""
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
    _TASK = None


async def _run_forever() -> None:
    await asyncio.sleep(FIRST_DELAY_SECONDS)
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("learning_scheduler_failed err_type=%s", type(exc).__name__)
        await asyncio.sleep(CHECK_EVERY_SECONDS)


def candidates(min_evidence: int, since: datetime) -> list[tuple[str, str]]:
    """(workspace, repo slug) pairs with at least `min_evidence` distinct
    (PR, person) signals since `since`. Blocking."""
    from sqlalchemy import select

    from src.db.models import FindingSignal as M
    from src.review.learning import signals as sig

    with sig._session() as s:
        rows = s.execute(select(
            M.workspace_id, M.repo_slug, M.pr_provider, M.pr_repo, M.pr_number, M.actor,
        ).where(M.created_at >= since).distinct().limit(50_000)).all()
    counts: dict[tuple[str, str], int] = {}
    for ws, slug, *_rest in rows:
        counts[(ws, slug)] = counts.get((ws, slug), 0) + 1
    return sorted(k for k, n in counts.items() if n >= max(1, int(min_evidence)))


async def run_once(now: datetime | None = None) -> dict:
    """One pass. Returns {"checked", "started"}."""
    from src.review import rules_generate
    from src.review.learning import signals as sig
    from src.review.settings import get_review_settings

    settings = get_review_settings()
    if settings.learning_rules_schedule != "weekly":
        return {"checked": 0, "started": 0}
    now = now or datetime.now(UTC)
    found = await asyncio.to_thread(
        candidates, settings.learning_rules_min_evidence,
        sig.window_start(settings.learning_window_days))
    started = 0
    for ws, slug in found[:MAX_PER_PASS]:
        jobs = await rules_generate.list_jobs(ws, slug, limit=10)
        recent = [j for j in jobs if j["kind"] == "history" and j.get("created_at")
                  and now - datetime.fromisoformat(j["created_at"]) < MIN_GAP]
        if recent:
            continue
        job, created = await rules_generate.start_job(ws, slug, "history", "system")
        if created:
            await rules_generate.run_job(job["id"], "system")
            started += 1
    return {"checked": len(found), "started": started}
