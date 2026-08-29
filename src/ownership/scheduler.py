"""Nightly ownership rebuild — one asyncio background task, no extra deps.

Runs a full rebuild across every indexed repo once per 24h (default) so the
ownership graph doesn't rot between manual button clicks. Design:

  * asyncio task started from FastAPI startup handler
  * per-repo `compute_ownership` runs in a thread (sync git blame)
  * staggered — 30s gap between repos to avoid pegging git/CPU
  * skips repos with no local clone (nothing to blame)
  * next run scheduled after previous batch finishes (drift-tolerant)

We keep asyncio-only to avoid adding APScheduler; the review poller uses
the same pattern.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_TASK: asyncio.Task | None = None


def start_ownership_scheduler() -> None:
    """Kick off the background loop. Idempotent."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    _TASK = loop.create_task(_run_forever())
    logger.info("ownership_scheduler_started")


async def _run_forever() -> None:
    interval_hours = float(os.environ.get("CELMIS_OWNERSHIP_INTERVAL_HOURS", "24"))
    stagger_seconds = float(os.environ.get("CELMIS_OWNERSHIP_STAGGER_SECONDS", "30"))
    # Delay first run so it doesn't compete with startup work.
    await asyncio.sleep(60.0)
    while True:
        started = datetime.now(UTC)
        try:
            await _rebuild_all_once(stagger=stagger_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ownership_scheduler_iter_failed err=%s", exc)
        # Stage 21 — piggyback nightly audit retention on the same loop.
        try:
            from src.api.routers.audit import purge_expired_audit
            deleted = await asyncio.get_event_loop().run_in_executor(
                None, purge_expired_audit,
            )
            if deleted:
                logger.info("audit_retention_purged files=%d", deleted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit_retention_failed err=%s", exc)
        # Alert retention, on the same loop and for the same reason as the
        # audit purge above: `incoming_alerts` had no DELETE and no sweep, so
        # whatever arrived stayed for the life of the installation. An alert
        # body is somebody else's text about a failure and can name a person,
        # which makes "kept for ever" an answer nobody wants to give to an
        # erasure request.
        try:
            from src.api.routers.alerts import purge_expired_alerts
            deleted = await asyncio.get_event_loop().run_in_executor(
                None, purge_expired_alerts,
            )
            if deleted:
                logger.info("alert_retention_purged rows=%d", deleted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert_retention_failed err=%s", exc)
        elapsed = (datetime.now(UTC) - started).total_seconds()
        sleep_for = max(60.0, interval_hours * 3600.0 - elapsed)
        logger.info(
            "ownership_scheduler_sleep next_in_seconds=%.0f", sleep_for,
        )
        await asyncio.sleep(sleep_for)


async def _rebuild_all_once(*, stagger: float) -> None:
    """Enqueue one durable job per repo (idempotent via dedup_key).

    Previously ran compute inline — that meant a mid-rebuild crash lost
    the whole batch and there was no visibility into progress. Now the
    worker picks each job up, retries on failure, and surfaces in
    /admin/jobs.
    """
    slugs = _list_indexed_repo_slugs()
    if not slugs:
        logger.info("ownership_scheduler_noop no_repos_indexed")
        return
    logger.info("ownership_scheduler_iter_start repos=%d", len(slugs))
    from src.sync.queue import KIND_OWNERSHIP_REBUILD, enqueue
    for slug in slugs:
        try:
            enqueue(
                kind=KIND_OWNERSHIP_REBUILD,
                payload={"repo_slug": slug, "lookback_days": 90},
                dedup_key=f"ownership:{slug}",
                enqueued_by="ownership_scheduler",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ownership_enqueue_failed repo=%s err=%s", slug, exc)
        await asyncio.sleep(stagger)
    logger.info("ownership_scheduler_iter_done enqueued=%d", len(slugs))


def _compute_or_none(slug: str) -> str | None:
    from src.ownership.builder import compute_ownership
    return compute_ownership(slug, lookback_days=90, computed_by="scheduler")


def _list_indexed_repo_slugs() -> list[str]:
    """Every repo present in the local clones directory. Uses the same
    settings the ownership builder uses, so a repo listed here is guaranteed
    to have a chance at blame data.
    """
    try:
        from src.config import get_settings
        settings = get_settings()
        base = settings.clones_dir if hasattr(settings, "clones_dir") else None
        if base is None:
            # Fall back to probing repo_path for a curated set — safer than
            # scanning arbitrary disk. Read repo list from RepoAutoReviewStore.
            from src.api.auto_review import get_auto_review_store
            store = get_auto_review_store()
            return [r.repo_slug for r in store.list_all()]
        from pathlib import Path
        base = Path(base)
        if not base.exists():
            return []
        # slug = <owner>/<repo> — clones typically nested one deep. Best-effort.
        slugs: list[str] = []
        for owner_dir in base.iterdir():
            if not owner_dir.is_dir():
                continue
            for repo_dir in owner_dir.iterdir():
                if (repo_dir / ".git").exists():
                    slugs.append(f"{owner_dir.name}/{repo_dir.name}")
        return slugs
    except Exception as exc:  # noqa: BLE001
        logger.warning("ownership_repo_enum_failed err=%s", exc)
        return []


__all__ = ["start_ownership_scheduler"]
