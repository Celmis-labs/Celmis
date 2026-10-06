"""The command ledger: one row per comment the receiver accepted as a command.

Three jobs, one table (`pr_command_events`):

* Idempotency. `claim` is an INSERT against the unique key (workspace,
  provider, repo, PR, comment id). A redelivery, a queue retry, or an edit of
  a comment that was already handled inserts nothing and returns None — the
  second delivery of a command is dropped, not answered twice.
* The rate limits, counted from the rows themselves, so they hold across
  restarts and across workers.
* The timeline on the pull-requests page.

Sync engine, like `issues`; every function takes an optional `engine` for the
tests. `claim` and `finish` raise on a database error — the receiver treats an
unreadable ledger as "do nothing" (a command that cannot be recorded cannot be
made idempotent) — while the read helpers fail soft.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from src.review.issues import _engine

logger = logging.getLogger(__name__)

CLAIMED = "claimed"
DONE = "done"
DENIED = "denied"
RATE_LIMITED = "rate_limited"
FAILED = "failed"
IGNORED = "ignored"

#: A row in one of these states cost the bot no reply (or none we count).
_SILENT = (RATE_LIMITED, IGNORED)

#: The arguments kept in the ledger; the discussion around a command is not.
_ARGS_CHARS = 500


def claim(
    workspace_id: str, provider: str, repo: str, pr_number: int, comment_id: str, *,
    command: str, args: str = "", force: bool = False, parent_id: str | None = None,
    event_key: str | None = None, actor_id: str | None = None,
    actor_name: str | None = None, status: str = CLAIMED, engine=None,
) -> str | None:
    """Record the command; its row id, or None when this comment was handled before."""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    from src.db.models import PRCommandEvent

    with Session(engine or _engine()) as s:
        row = PRCommandEvent(
            workspace_id=workspace_id, provider=provider, repo=repo,
            pr_number=int(pr_number), comment_id=str(comment_id), parent_id=parent_id,
            event_key=event_key, command=command, args=(args or "")[:_ARGS_CHARS] or None,
            force=bool(force), actor_id=actor_id, actor_name=actor_name,
            status=status, created_at=datetime.now(UTC),
        )
        s.add(row)
        try:
            s.flush()
            row_id = row.id
            s.commit()
        except IntegrityError:
            s.rollback()
            return None
    return row_id


def finish(
    row_id: str | None, status: str, *, error: str | None = None,
    reply_comment_id: str | None = None, run_id: str | None = None, engine=None,
) -> None:
    """Close the row. Never raises: a ledger that cannot be updated must not
    turn a command that worked into a failure."""
    if not row_id:
        return
    try:
        from sqlalchemy.orm import Session

        from src.db.models import PRCommandEvent

        with Session(engine or _engine()) as s:
            row = s.get(PRCommandEvent, row_id)
            if row is None:
                return
            row.status = status
            row.finished_at = datetime.now(UTC)
            if error is not None:
                row.error = error[:500]
            if reply_comment_id is not None:
                row.reply_comment_id = str(reply_comment_id)
            if run_id is not None:
                row.run_id = str(run_id)
            s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("command_ledger_finish_failed row=%s err=%s", row_id, exc)


def count_recent(
    workspace_id: str, *, provider: str | None = None, repo: str | None = None,
    pr_number: int | None = None, actor_id: str | None = None,
    window: timedelta = timedelta(hours=1), now: datetime | None = None,
    status: str | None = None, forced: bool | None = None,
    without: tuple[str, ...] = (), engine=None,
) -> int:
    """Rows in the window. Without `status`, the rows that cost a reply.

    `without` leaves more statuses out (a stranger's `denied` rows must not eat
    the budget a pull request's maintainers share); `forced` keeps only the
    forced (True) or only the plain (False) commands.

    Give `repo` + `pr_number` for the per-PR limit, `actor_id` for the
    per-person one. 0 on a database error: a limit that cannot be read is not
    a reason to refuse every command.
    """
    try:
        from sqlalchemy import func, select
        from sqlalchemy.orm import Session

        from src.db.models import PRCommandEvent

        since = (now or datetime.now(UTC)) - window
        stmt = select(func.count()).select_from(PRCommandEvent).where(
            PRCommandEvent.workspace_id == workspace_id,
            PRCommandEvent.created_at >= since,
        )
        if provider is not None:
            stmt = stmt.where(PRCommandEvent.provider == provider)
        if repo is not None:
            stmt = stmt.where(PRCommandEvent.repo == repo)
        if pr_number is not None:
            stmt = stmt.where(PRCommandEvent.pr_number == int(pr_number))
        if actor_id is not None:
            stmt = stmt.where(PRCommandEvent.actor_id == actor_id)
        if status is not None:
            stmt = stmt.where(PRCommandEvent.status == status)
        else:
            stmt = stmt.where(PRCommandEvent.status.notin_((*_SILENT, *without)))
        if forced is not None:
            stmt = stmt.where(PRCommandEvent.force.is_(forced))
        with Session(engine or _engine()) as s:
            return int(s.execute(stmt).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("command_ledger_count_failed err=%s", exc)
        return 0


def for_pr(
    workspace_id: str, provider: str, repo: str, pr_number: int, *,
    limit: int = 50, engine=None,
) -> list[dict]:
    """The pull request's commands, newest first, for the timeline."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import PRCommandEvent

    stmt = (
        select(PRCommandEvent)
        .where(
            PRCommandEvent.workspace_id == workspace_id,
            PRCommandEvent.provider == provider,
            PRCommandEvent.repo == repo,
            PRCommandEvent.pr_number == int(pr_number),
        )
        .order_by(PRCommandEvent.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
    )
    with Session(engine or _engine()) as s:
        return [
            {
                "id": r.id,
                "comment_id": r.comment_id,
                "command": r.command,
                "args": r.args,
                "force": bool(r.force),
                "actor_name": r.actor_name,
                "status": r.status,
                "error": r.error,
                "run_id": r.run_id,
                "created_at": r.created_at,
                "finished_at": r.finished_at,
            }
            for r in s.execute(stmt).scalars()
        ]
