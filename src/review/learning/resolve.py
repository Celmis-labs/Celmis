"""A person resolving (or reopening) the thread of one of our findings.

Resolving says little — the code may have been fixed, or the person may just
want the thread out of the way — so it is a weak signal (0.4). What becomes of
it is decided later: a following commit that implements the finding upgrades it
to `implemented` (the issues ledger tells us through `outcome_hooks`), and a
merge with the code unchanged turns it into a weak dismissal. Reopening
withdraws it.

The webhook receiver extracts the event with `extract_*` (pure; `None` for
anything that is not a thread event of ours to read) and calls
`handle_thread_event`, which never raises.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.review.learning import signals as sig

logger = logging.getLogger(__name__)


@dataclass
class ThreadEvent:
    """A thread was resolved or reopened."""

    provider: str
    repo: str
    pr_number: int
    resolved: bool
    comment_ids: list[str] = field(default_factory=list)
    actor_id: str = ""
    actor_name: str = ""
    actor_is_bot: bool = False
    #: True/False when the payload says; None when it does not.
    repo_private: bool | None = None


# Threads the reviewer closed itself (an outdated finding after a push). On a
# human token GitHub reports that as a person resolving the thread, so the
# threads are remembered here, just before they are closed, for as long as a
# delivery can take to arrive. Held in memory: one backend process serves the
# webhooks and runs the reviews.
_CLOSED_BY_US_SECONDS = 600.0
_closed_by_us: dict[tuple[str, str, int, str], float] = {}
_closed_lock = threading.Lock()


def note_closed_by_us(provider: str, repo: str, pr_number: int, comment_ids) -> None:
    """The reviewer is about to resolve the threads rooted at these comments."""
    now = time.monotonic()
    with _closed_lock:
        for key in [k for k, until in _closed_by_us.items() if until <= now]:
            del _closed_by_us[key]
        for cid in comment_ids or ():
            _closed_by_us[(provider, repo, int(pr_number), str(cid))] = now + _CLOSED_BY_US_SECONDS


def was_closed_by_us(ev: ThreadEvent) -> bool:
    """True when the thread of this `resolved` event is one the reviewer closed."""
    now = time.monotonic()
    with _closed_lock:
        return any(
            _closed_by_us.get((ev.provider, ev.repo, int(ev.pr_number), str(cid)), 0.0) > now
            for cid in ev.comment_ids)


def extract_github_thread_event(payload: dict) -> ThreadEvent | None:
    """A `pull_request_review_thread` delivery (action resolved | unresolved)."""
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    if action not in ("resolved", "unresolved"):
        return None
    thread = payload.get("thread") or {}
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name") or ""
    number = pr.get("number")
    ids = [str(c.get("id")) for c in thread.get("comments") or []
           if isinstance(c, dict) and c.get("id") is not None]
    if not repo or not isinstance(number, int) or not ids:
        return None
    sender = payload.get("sender") or {}
    return ThreadEvent(
        provider="github", repo=str(repo), pr_number=number, resolved=action == "resolved",
        comment_ids=ids, actor_id=str(sender.get("login") or ""),
        actor_name=str(sender.get("login") or ""),
        actor_is_bot=str(sender.get("type") or "").lower() == "bot",
        repo_private=_private(payload.get("repository")))


def _private(repository: Any) -> bool | None:
    value = repository.get("private") if isinstance(repository, dict) else None
    return value if isinstance(value, bool) else None


def handle_thread_event(
    ev: ThreadEvent, *, workspace_id: str, session: Any = None,
    permitted: Callable[[], bool] | None = None,
) -> dict:
    """Record or withdraw the resolve signal of the finding the thread holds.
    Returns {handled, action}. Never raises.

    `permitted` is asked once, only for a thread that holds one of our
    findings and was resolved: whether this person may teach the reviewer here
    (a person who reopens only takes back what they gave)."""
    try:
        if ev is None or ev.actor_is_bot or not ev.comment_ids:
            return {"handled": False, "action": "ignored"}
        if ev.resolved and was_closed_by_us(ev):
            return {"handled": False, "action": "closed by the reviewer"}
        pr = sig.PRRef(ev.provider, ev.repo, int(ev.pr_number))
        posted = None
        for cid in ev.comment_ids:
            posted = sig.find_posted(workspace_id, pr, cid, session=session)
            if posted is not None:
                break
        if posted is None:
            return {"handled": False, "action": "not a finding"}
        who = sig.normalise_actor(ev.actor_id or ev.actor_name)
        repo_slug = posted.get("repo_slug") or sig.local_slug(pr.provider, pr.repo)
        snap = sig.snapshot_of_posted(posted)
        if not ev.resolved:
            n = _withdraw(workspace_id, snap.fingerprint, pr, who, session)
            return {"handled": True, "action": "reopened", "withdrawn": n}
        if permitted is not None and not permitted():
            return {"handled": False, "action": "not allowed to teach here"}
        wrote = sig.record_signal(
            workspace_id, repo_slug, snap, "resolved", "resolve", pr=pr, actor=who,
            comment_id=str(posted["comment_id"]), run_id=posted.get("run_id"),
            sha=posted.get("sha"), session=session)
        return {"handled": True, "action": "resolved", "signal": wrote}
    except Exception as exc:  # noqa: BLE001
        logger.warning("learning_resolve_failed ws=%s err_type=%s", workspace_id,
                       type(exc).__name__)
        return {"handled": False, "action": "error"}


def _withdraw(ws: str, fingerprint: str, pr: sig.PRRef, who: str, session: Any) -> int:
    """Remove this person's resolve signal (whatever the automatic upgrade made
    of it stays: that was the code's own observation)."""
    from sqlalchemy import delete

    from src.db.models import FindingSignal as M

    with sig._session(session) as s:
        res = s.execute(delete(M).where(
            *sig._key_filter(M, ws, pr, fingerprint), M.source == "resolve",
            M.actor == who, M.signal == "resolved"))
        s.commit()
        return int(res.rowcount or 0)
