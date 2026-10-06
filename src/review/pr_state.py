"""The per-PR state the review cadence works from, on `review_pull_requests`.

* `register_push` — a delivery named a head: was it a push, and did it tip the
  PR into auto-pause? One transaction with the row locked (`issues._pr_row`),
  so two deliveries for one PR cannot both count the same window.
* `mark_reviewed` — a COMPLETE, posted review read this head; the baseline an
  incremental review starts from.
* `set_paused` / `resume` — the interface of the comment commands (`pause`,
  `start-review`), the Pause / Resume buttons and the auto-pause itself.
* `claim_notice` — exactly one caller gets to post the "paused" note.

Sync engine, like `issues`; every function takes an optional `engine` so the
tests run on SQLite. Functions that decide a gate fail OPEN in their callers
(a review is never lost to an unreadable table); the ones here raise.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from src.review import cadence
from src.review.issues import _engine, _now, _pr_row

logger = logging.getLogger(__name__)


def same_sha(stored: str | None, delivered: str | None) -> bool:
    """Two commit ids of one commit — Bitbucket's webhook carries the first 12
    characters where its API (what a run stored) returns all 40, so equality
    alone would never match there. A prefix of at least 7 is one commit."""
    a, b = (stored or "").lower(), (delivered or "").lower()
    short = min(len(a), len(b))
    return short >= 7 and a[:short] == b[:short]


@dataclass(frozen=True)
class PRState:
    last_reviewed_sha: str | None = None
    last_reviewed_at: datetime | None = None
    last_seen_sha: str | None = None
    review_paused: bool = False
    paused_reason: str | None = None
    paused_at: datetime | None = None
    paused_by: str | None = None
    pause_notice_at: datetime | None = None
    recent_pushes: int = 0


@dataclass(frozen=True)
class PushResult:
    #: The delivery named a head the PR had not been seen at.
    is_push: bool
    #: The PR is paused now (this push included).
    paused: bool
    #: This very push paused it.
    newly_paused: bool
    paused_reason: str | None
    last_reviewed_sha: str | None
    #: Pushes inside the window, this one included.
    pushes: int
    #: Nobody has posted the "reviews are paused" note since the pause began.
    notice_pending: bool = True


def _key(workspace_id: str, provider: str, repo: str, number: int):
    from src.db.models import ReviewPullRequest as R

    return (R.workspace_id == workspace_id, R.provider == provider,
            R.repo == repo, R.number == int(number))


def load(workspace_id: str, provider: str, repo: str, number: int, *,
         engine=None) -> PRState | None:
    """The PR's cadence state; None for a PR never seen. Read-only."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import ReviewPullRequest as R

    with Session(engine or _engine()) as s:
        row = s.execute(select(R).where(*_key(workspace_id, provider, repo, number))
                        ).scalar_one_or_none()
        if row is None:
            return None
        return PRState(
            last_reviewed_sha=row.last_reviewed_sha,
            last_reviewed_at=row.last_reviewed_at,
            last_seen_sha=row.last_seen_sha,
            review_paused=bool(row.review_paused),
            paused_reason=row.paused_reason,
            paused_at=row.paused_at,
            paused_by=row.paused_by,
            pause_notice_at=row.pause_notice_at,
            recent_pushes=len(row.recent_pushes or []),
        )


def _fill_meta(row, meta: dict | None) -> None:
    """A row born from a delivery gets the title and links the delivery
    carried, so the pull-requests page never lists an unnamed PR."""
    for key, limit in (("title", 500), ("author", None), ("url", None),
                       ("head_ref", None), ("base_ref", None)):
        value = (meta or {}).get(key)
        if value and not getattr(row, key, None):
            setattr(row, key, str(value)[:limit] if limit else str(value))


def register_push(
    workspace_id: str, provider: str, repo: str, number: int, head_sha: str, *,
    cadence_name: str = "automatic", limit: int = 3, window_minutes: int = 15,
    now: datetime | None = None, meta: dict | None = None, engine=None,
) -> PushResult:
    """Count a delivery's head. The same head as last time is not a push (a
    title edit, a reopened PR). Under `auto_pause`, the push that makes
    `limit` inside the window pauses the PR — and is itself skipped."""
    from sqlalchemy.orm import Session

    now = now or _now()
    with Session(engine or _engine()) as s:
        row = _pr_row(s, workspace_id, provider, repo, int(number))
        _fill_meta(row, meta)
        if (row.review_paused and row.paused_reason == cadence.REASON_AUTO
                and cadence_name != "auto_pause"):
            # The repository left `auto_pause`: that pause lapsed. Forget it,
            # so a later switch back does not revive it without a push burst.
            _clear_pause(row)
        pushes = cadence.prune(row.recent_pushes, now, window_minutes)
        is_push = bool(head_sha) and not same_sha(row.last_seen_sha, head_sha)
        newly = False
        if is_push:
            row.last_seen_sha = head_sha
            pushes.append(now)
            if (cadence_name == "auto_pause" and not row.review_paused
                    and cadence.should_pause(len(pushes), limit)):
                row.review_paused = True
                row.paused_reason = cadence.REASON_AUTO
                row.paused_at = now
                row.paused_by = None
                row.pause_notice_at = None
                newly = True
        row.recent_pushes = [p.isoformat() for p in pushes]
        result = PushResult(
            is_push=is_push, paused=bool(row.review_paused), newly_paused=newly,
            paused_reason=row.paused_reason, last_reviewed_sha=row.last_reviewed_sha,
            pushes=len(pushes), notice_pending=row.pause_notice_at is None,
        )
        s.commit()
    return result


def mark_reviewed(workspace_id: str, provider: str, repo: str, number: int,
                  head_sha: str, *, now: datetime | None = None, engine=None) -> bool:
    """A complete, posted review read `head_sha`. False when there is no row
    or no sha (nothing was written)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import ReviewPullRequest as R

    if not head_sha:
        return False
    with Session(engine or _engine()) as s:
        row = s.execute(select(R).where(*_key(workspace_id, provider, repo, number))
                        ).scalar_one_or_none()
        if row is None:
            return False
        row.last_reviewed_sha = head_sha
        row.last_reviewed_at = now or _now()
        s.commit()
    return True


def set_paused(workspace_id: str, provider: str, repo: str, number: int, *,
               paused: bool = True, reason: str = cadence.REASON_MANUAL,
               by: str | None = None, now: datetime | None = None, engine=None) -> bool:
    """Pause (or, with `paused=False`, resume) a PR's automatic reviews.
    Pausing creates the row when the PR was never seen. True when the stored
    state changed."""
    if paused and reason not in cadence.PAUSE_REASONS:
        raise ValueError(f"unknown pause reason: {reason!r}")
    from sqlalchemy.orm import Session

    now = now or _now()
    with Session(engine or _engine()) as s:
        row = _pr_row(s, workspace_id, provider, repo, int(number))
        if paused:
            changed = not row.review_paused
            row.review_paused = True
            if changed:
                row.paused_reason = reason
                row.paused_at = now
                row.paused_by = by
                row.pause_notice_at = None
        else:
            changed = bool(row.review_paused)
            _clear_pause(row)
        s.commit()
    return changed


def _clear_pause(row) -> None:
    row.review_paused = False
    row.paused_reason = None
    row.paused_at = None
    row.paused_by = None
    row.pause_notice_at = None
    # The pushes that paused the PR must not pause it again at once.
    row.recent_pushes = []


def resume(workspace_id: str, provider: str, repo: str, number: int, *,
           engine=None) -> bool:
    """Clear the pause. True when the PR was paused (the caller then queues
    the review that covers every skipped push, as `ReviewRequest(resume=True)`)."""
    return set_paused(workspace_id, provider, repo, number, paused=False, engine=engine)


def claim_notice(workspace_id: str, provider: str, repo: str, number: int, *,
                 now: datetime | None = None, engine=None) -> bool:
    """True for exactly one caller per pause: the one that now posts the
    "reviews are paused" note. A cadence-`manual` PR has no pause row, so the
    claim is on the same column: once per PR until a resume clears it."""
    from sqlalchemy.orm import Session

    with Session(engine or _engine()) as s:
        row = _pr_row(s, workspace_id, provider, repo, int(number))
        if row.pause_notice_at is not None:
            s.rollback()
            return False
        row.pause_notice_at = now or _now()
        s.commit()
    return True


def release_notice(workspace_id: str, provider: str, repo: str, number: int, *,
                   engine=None) -> None:
    """Give a claim back (the note could not be posted), so the next delivery
    tries again. Never raises."""
    try:
        from sqlalchemy.orm import Session

        with Session(engine or _engine()) as s:
            row = _pr_row(s, workspace_id, provider, repo, int(number))
            row.pause_notice_at = None
            s.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pause_notice_release_failed pr=%s err=%s", number, exc)
