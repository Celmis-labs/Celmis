"""The read cache: one Jira fetch shared by every worker process.

Rows live in Postgres (`task_context_cache`), keyed (workspace, Jira site,
issue key), so a PR re-reviewed on every push — or ten PRs naming the same
epic — costs one request, whichever worker runs them. The staleness rule is
the service's (service.py); this module only stores and fetches rows.

Best effort by design: a missing database, a missing table (an install that
has not run the migration yet) or a failed write is logged and answered as
"nothing cached". The cache makes reviews cheaper; it must never make one
fail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: Rows older than this are dropped by `prune`.
RETENTION_DAYS = 30


@dataclass
class CachedIssue:
    """A stored read: the payload, how it ended and when it was fetched."""

    payload: dict[str, Any]
    status: str                 # ok | not_found | forbidden
    issue_updated: str | None
    fetched_at: datetime

    def age_seconds(self, now: datetime | None = None) -> float:
        stamp = self.fetched_at
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return ((now or datetime.now(UTC)) - stamp).total_seconds()


def _engine():
    from src.review.issues import _engine as engine

    return engine()


def get(workspace_id: str, site_host: str, issue_key: str, *, engine: Any = None) -> CachedIssue | None:
    """The stored read of `issue_key`, or None (never raises)."""
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from src.db.models import TaskContextCache

        with Session(engine or _engine()) as s:
            row = s.execute(select(TaskContextCache).where(
                TaskContextCache.workspace_id == workspace_id,
                TaskContextCache.site_host == site_host,
                TaskContextCache.issue_key == issue_key,
            )).scalar_one_or_none()
            if row is None or not isinstance(row.payload, dict):
                return None
            return CachedIssue(
                payload=dict(row.payload), status=str(row.status or "ok"),
                issue_updated=row.issue_updated, fetched_at=row.fetched_at,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("task_cache_read_failed key=%s err=%s", issue_key, type(exc).__name__)
        return None


def put(
    workspace_id: str, site_host: str, issue_key: str, payload: dict[str, Any], *,
    status: str = "ok", issue_updated: str | None = None, engine: Any = None,
) -> bool:
    """Store (or replace) a read. True when written (never raises)."""
    try:
        import uuid

        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from src.db.models import TaskContextCache

        now = datetime.now(UTC)
        with Session(engine or _engine()) as s:
            row = s.execute(select(TaskContextCache).where(
                TaskContextCache.workspace_id == workspace_id,
                TaskContextCache.site_host == site_host,
                TaskContextCache.issue_key == issue_key,
            )).scalar_one_or_none()
            if row is None:
                row = TaskContextCache(
                    id=str(uuid.uuid4()), workspace_id=workspace_id,
                    site_host=site_host, issue_key=issue_key,
                )
                s.add(row)
            row.payload = payload
            row.status = status
            row.issue_updated = issue_updated
            row.fetched_at = now
            s.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("task_cache_write_failed key=%s err=%s", issue_key, type(exc).__name__)
        return False


def touch(workspace_id: str, site_host: str, issue_key: str, *, engine: Any = None) -> None:
    """The stored read was confirmed current: restart its hot window."""
    try:
        from sqlalchemy import update
        from sqlalchemy.orm import Session

        from src.db.models import TaskContextCache

        with Session(engine or _engine()) as s:
            s.execute(update(TaskContextCache).where(
                TaskContextCache.workspace_id == workspace_id,
                TaskContextCache.site_host == site_host,
                TaskContextCache.issue_key == issue_key,
            ).values(fetched_at=datetime.now(UTC)))
            s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("task_cache_touch_failed key=%s err=%s", issue_key, type(exc).__name__)


def prune(*, days: int = RETENTION_DAYS, engine: Any = None) -> int:
    """Drop rows older than `days`; the number dropped (0 on failure)."""
    try:
        from sqlalchemy import delete
        from sqlalchemy.orm import Session

        from src.db.models import TaskContextCache

        cutoff = datetime.now(UTC) - timedelta(days=days)
        with Session(engine or _engine()) as s:
            result = s.execute(delete(TaskContextCache).where(
                TaskContextCache.fetched_at < cutoff))
            s.commit()
            return int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("task_cache_prune_failed err=%s", type(exc).__name__)
        return 0


def purge_workspace(workspace_id: str, *, engine: Any = None) -> int:
    """Drop every stored read of one workspace; the number dropped (0 on
    failure). The key holds no credential identity, so a connection that is
    removed or replaced must not leave behind what the old token could see."""
    try:
        from sqlalchemy import delete
        from sqlalchemy.orm import Session

        from src.db.models import TaskContextCache

        with Session(engine or _engine()) as s:
            result = s.execute(delete(TaskContextCache).where(
                TaskContextCache.workspace_id == workspace_id))
            s.commit()
            return int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("task_cache_purge_failed err=%s", type(exc).__name__)
        return 0
